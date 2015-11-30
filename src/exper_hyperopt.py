import logging
import argparse
import sys, time, datetime
from itertools import *
import random, numpy as np

from sklearn.metrics import confusion_matrix, classification_report

import theano
import lasagne
import theano.tensor as T

import featchar
import rep
import encoding
from utils import get_sents, sample_sents, valid_file_name, break2subsents
from utils import ROOT_DIR
from score import conlleval
from lazrnn import RDNN, RDNN_Dummy, extract_rnn_params
from hyperopt import hp, fmin, tpe, Trials, STATUS_OK
from copy import deepcopy

LOG_DIR = '{}/logs'.format(ROOT_DIR)
random.seed(0)
rng = np.random.RandomState(1234567)
lasagne.random.set_rng(rng)

def get_arg_parser():
    parser = argparse.ArgumentParser(prog="lazrnn")
    
    parser.add_argument("--rnn", default='lazrnn', choices=['dummy','lazrnn','nerrnn'], help="which rnn to use")
    parser.add_argument("--rep", default='std', choices=['std','nospace','spec'], help="which representation to use")
    parser.add_argument("--activation", default='bi-lstm', help="activation function for hidden layer: bi-relu bi-lstm bi-tanh")
    parser.add_argument("--fbmerge", default='concat', choices=['concat','sum'], help="how to merge forward backward layer outputs")
    parser.add_argument("--n_hidden", default=[128], nargs='+', type=int, help="number of neurons in each hidden layer")
    parser.add_argument("--recout", default=1, type=int, help="use recurrent output layer")
    parser.add_argument("--batch_norm", default=0, type=int, help="whether to use batch norm between deep layers")
    parser.add_argument("--drates", default=[0, 0], nargs='+', type=float, help="dropout rates")
    parser.add_argument("--opt", default="adam", help="optimization method: sgd, rmsprop, adagrad, adam")
    parser.add_argument("--lr", default=0.001, type=float, help="learning rate")
    parser.add_argument("--norm", default=5, type=float, help="Threshold for clipping norm of gradient")
    parser.add_argument("--n_batch", default=32, type=int, help="batch size")
    parser.add_argument("--fepoch", default=50, type=int, help="number of epochs")
    parser.add_argument("--patience", default=-1, type=int, help="how patient the validator is")
    parser.add_argument("--sample", default=0, type=int, help="num of sents to sample from trn in the order of K")
    parser.add_argument("--feat", default='basic', help="feat func to use")
    parser.add_argument("--emb", default=0, type=int, help="embedding layer size")
    parser.add_argument("--gclip", default=0, type=float, help="clip gradient messages in recurrent layers if they are above this value")
    parser.add_argument("--truncate", default=-1, type=int, help="backward step size")
    parser.add_argument("--log", default='das_auto', help="log file name")
    parser.add_argument("--sorted", default=1, type=int, help="sort datasets before training and prediction")
    parser.add_argument("--in2out", default=0, type=int, help="connect input & output")
    parser.add_argument("--curriculum", default=1, type=int, help="curriculum learning: number of parts")
    parser.add_argument("--lang", default='eng', help="ner lang")
    parser.add_argument("--save", default=False, action='store_true', help="save param values to file")
    parser.add_argument("--shuf", default=1, type=int, help="shuffle the batches.")
    parser.add_argument("--tagging", default='io', help="tag scheme to use")
    parser.add_argument("--reverse", default=False, action='store_true', help="reverse the training data as additional data")
    parser.add_argument("--decoder", default=0, type=int, help="use decoder to prevent invalid tag transitions")
    parser.add_argument("--breaktrn", default=0, type=int, help="break trn sents to subsents")
    parser.add_argument("--captrn", default=0, type=int, help="consider sents lt this as trn")
    parser.add_argument("--fbias", default=0., type=float, help="forget gate bias")
    parser.add_argument("--gnoise", default=False, action='store_true', help="adding time dependent noise to the gradients")

    return parser



class Batcher(object):

    def __init__(self, batch_size, feat):
        self.batch_size = batch_size
        self.feat = feat

    def get_batches(self, dset):
        nf = self.feat.NF 
        sent_batches = [dset[i:i+self.batch_size] for i in range(0, len(dset), self.batch_size)]
        X_batches, Xmsk_batches, y_batches, ymsk_batches = [], [], [], []
        batches = []
        for batch in sent_batches:
            mlen = max(len(sent['cseq']) for sent in batch)
            X_batch = np.zeros((len(batch), mlen, nf),dtype=theano.config.floatX)
            Xmsk_batch = np.zeros((len(batch), mlen),dtype=np.bool)
            y_batch = np.zeros((len(batch), mlen, self.feat.NC),dtype=theano.config.floatX)
            ymsk_batch = np.zeros((len(batch), mlen, self.feat.NC),dtype=np.bool)
            for si, sent in enumerate(batch):
                Xsent, ysent = self.feat.transform(sent)
                nchar = Xsent.shape[0]
                X_batch[si,:nchar,:] = Xsent
                Xmsk_batch[si,:nchar] = True
                y_batch[si,:nchar,:] = ysent
                ymsk_batch[si,:nchar,:] = True
            batches.append((X_batch, Xmsk_batch, y_batch, ymsk_batch))
        return batches
        """
            X_batches.append(X_batch)
            Xmsk_batches.append(Xmsk_batch)
            y_batches.append(y_batch)
            ymsk_batches.append(ymsk_batch)
        return X_batches, Xmsk_batches, y_batches, ymsk_batches
        """

class Reporter(object):

    def __init__(self, feat, tfunc):
        self.feat = feat
        self.tfunc = tfunc

    def report(self, dset, pred):
        y_true = self.feat.tseqenc.transform([t for sent in dset for t in sent['tseq']])
        y_pred = list(chain.from_iterable(pred))
        cerr = np.sum(y_true!=y_pred)/float(len(y_true))

        char_conmat_str = self.get_conmat_str(y_true, y_pred, self.feat.tseqenc)

        lts = [sent['ts'] for sent in dset]
        lts_pred = []
        for sent, ipred in zip(dset, pred):
            tseq_pred = self.feat.tseqenc.inverse_transform(ipred)
            # tseqgrp_pred = get_tseqgrp(sent['wiseq'],tseq_pred)
            ts_pred = self.tfunc(sent['wiseq'],tseq_pred)
            lts_pred.append(ts_pred) # changed

        y_true = self.feat.tsenc.transform([t for ts in lts for t in ts])
        y_pred = self.feat.tsenc.transform([t for ts in lts_pred for t in ts])
        werr = np.sum(y_true!=y_pred)/float(len(y_true))

        word_conmat_str = self.get_conmat_str(y_true, y_pred, self.feat.tsenc)

        # wacc, pre, recall, f1 = bilouEval2(lts, lts_pred)
        (wacc, pre, recall, f1), conll_print = conlleval(lts, lts_pred)
        return cerr, werr, wacc, pre, recall, f1, conll_print, char_conmat_str, word_conmat_str

    def get_conmat_str(self, y_true, y_pred, lblenc):
        str_list = []
        str_list.append('\t'.join(['bos'] + list(lblenc.classes_)))
        conmat = confusion_matrix(y_true,y_pred, labels=lblenc.transform(lblenc.classes_))
        for r,clss in zip(conmat,lblenc.classes_):
            str_list.append('\t'.join([clss] + list(map(str,r))))
        return '\n'.join(str_list) + '\n'

class Validator(object):

    def __init__(self, trn, dev, tst, batcher, reporter):
        self.trn = trn
        self.dev = dev
        self.tst = tst
        self.trndat = batcher.get_batches(trn) 
        self.devdat = batcher.get_batches(dev) 
        self.tstdat = batcher.get_batches(tst) 
        self.reporter = reporter

    def validate(self, rdnn, argsd):
        logging.info('training the model...')
        dbests = {'trn':(1,0.), 'dev':(1,0.), 'tst':(1,0.)}
        decoder = 'viterbi' if argsd['decoder'] else 'predict'
        anger = 0
        for e in range(1,argsd['fepoch']+1): # foreach epoch
            logging.info(('{:<5} {:<5} ' + ('{:>10} '*10)).format('dset','epoch','mcost', 'mtime', 'cerr', 'werr', 'wacc', 'pre', 'recall', 'f1', 'best', 'best'))
            for funcname, ddat, datname in zip(['train',decoder,decoder],[self.trndat,self.devdat, self.tstdat],['trn','dev','tst']):
                if datname == 'tst' and dbests['dev'][0] != e:
                    continue
                if datname == 'trn' and argsd['shuf']:
                    logging.debug('shuffling trn batches...')
                    batch_ids = range(len(self.trndat))
                    random.shuffle(batch_ids)
                    ddat = map(ddat.__getitem__, batch_ids)
                start_time = time.time()
                mcost, pred = getattr(rdnn, funcname)(ddat)
                end_time = time.time()
                mtime = end_time - start_time

                if datname == 'trn' and argsd['shuf']:
                    logging.debug('restoring the order of trn batches...')
                    pred = [p for i, p in sorted(zip(batch_ids,pred))]
                pred = [p for b in pred for p in b]
                
                # cerr, werr, wacc, pre, recall, f1 = self.reporter.report(getattr(self.trn, pred) # find better solution for getattr
                cerr, werr, wacc, pre, recall, f1, conll_print, char_conmat_str, word_conmat_str =\
                        self.reporter.report(getattr(self, datname), pred) # find better solution for getattr
                
                if f1 > dbests[datname][1]:
                    dbests[datname] = (e,f1)
                    if argsd['save'] and datname == 'dev': # save model to file
                        param_log_name = ','.join(['{}:{}'.format(p,argsd[p]) for p in LPARAMS])
                        param_log_name = valid_file_name(param_log_name)
                        rnn_param_values = rdnn.get_param_values()
                        np.savez('{}/models/{}'.format(ROOT_DIR, param_log_name),rnn_param_values=rnn_param_values,args=argsd)

                
                logging.info(('{:<5} {:<5d} ' + ('{:>10.4f} '*9)+'{:>10d}')\
                    .format(datname,e,mcost, mtime, cerr, werr, wacc, pre, recall, f1, dbests[datname][1],dbests[datname][0]))
                
                logging.debug('')
                logging.debug(conll_print)
                logging.debug(char_conmat_str)
                logging.debug(word_conmat_str)
                logging.debug('')

            anger = 0 if e == dbests['dev'][0] else anger + 1
            if argsd['patience'] > 0 and anger > argsd['patience']:
                #logging.info('sabir tasti.')
                val = rdnn.lr.get_value()
                logging.info('old lr: {}, new lr: {}'.format(val, val * 0.95))
                rdnn.lr.set_value(val * 0.95)
                anger = 0
            logging.info('')
        return dbests['dev'][1]

class Curriculum(object):
    def __init__(self, trn, dev, batcher, reporter, numofparts):
        self.trn = trn
        self.dev = dev
        self.trndat = batcher.get_batches(trn)
        self.devdat = batcher.get_batches(dev)
        self.batcher = batcher
        self.reporter = reporter
        self.numofparts = numofparts
        self.trn_parts = []
        sentineachpart = len(self.trn) / self.numofparts
        
        for i in xrange(self.numofparts):
            l = i * sentineachpart
            u = (i + 1) * sentineachpart if i != (self.numofparts - 1) else len(self.trn)
            self.trn_parts.append(self.trn[l:u])

        self.trn_parts.append(self.trn)

    def validate(self, rdnn, fepoch, patience=-1):
        for i in xrange(self.numofparts + 1):
            logging.info('Learning part:{}'.format(i + 1))
            validator = Validator(self.trn_parts[i], self.dev, self.batcher, self.reporter)
            validator.validate(rdnn, fepoch, patience)
        
            

LPARAMS = ['activation', 'n_hidden', 'fbmerge', 'drates',
    'recout','decoder', 'opt','lr','norm','gclip','truncate','n_batch', 'shuf',
    'breaktrn', 'captrn', 'emb','lang', 'reverse','tagging', 'fbias']

def main():
    parser = get_arg_parser()
    args = vars(parser.parse_args())
    args['drates'] = args['drates'] if any(args['drates']) else [0]*(len(args['n_hidden'])+1)

    if args['rnn'] == 'nerrnn':
        args['n_batch'] = 1

    # logger setup
    import socket
    host = socket.gethostname().split('.')[0]
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    shandler = logging.StreamHandler()
    shandler.setLevel(logging.INFO)
    param_log_name = ','.join(['{}:{}'.format(p,args[p]) for p in LPARAMS])
    param_log_name = valid_file_name(param_log_name)
    base_log_name = '{}:{},{}'.format(host, theano.config.device, param_log_name if args['log'] == 'das_auto' else args['log'])
    ihandler = logging.FileHandler('{}/{}.info'.format(LOG_DIR,base_log_name), mode='w')
    ihandler.setLevel(logging.INFO)
    dhandler = logging.FileHandler('{}/{}.debug'.format(LOG_DIR,base_log_name), mode='w')
    dhandler.setLevel(logging.DEBUG)
    logger.addHandler(shandler);logger.addHandler(ihandler);logger.addHandler(dhandler);
    # end logger setup

    # print args
    for k,v in sorted(args.iteritems()):
        logger.info('{}:\t{}'.format(k,v))
    logger.info('{}:\t{}'.format('base_log_name',base_log_name))

    trn, dev, tst = get_sents(args['lang'])

    if args['breaktrn']:
        trn = [subsent for sent in trn for subsent in break2subsents(sent)]

    if args['captrn']:
        trn = filter(lambda sent: len(sent['ws'])<args['captrn'], trn)

    if args['sample']>0:
        trn_size = args['sample']*1000
        trn = sample_sents(trn,trn_size)

    repclass = getattr(rep, 'Rep'+args['rep'])
    repobj = repclass()

    for d in (trn,dev,tst):
        for sent in d:
            if args['tagging'] == 'io':
                sent['ts'] = encoding.any2io(sent['ts'])
            sent.update({
                'cseq': repobj.get_cseq(sent), 
                'wiseq': repobj.get_wiseq(sent), 
                'tseq': repobj.get_tseq(sent)})
    
    if args['reverse']:
        def reverse_data(d):
            copy_d = d.copy()
            
            for k in copy_d.keys():
                if k == "wiseq":
                    m = max(copy_d[k])
                    copy_d[k] = map(lambda x: -1 if x == -1 else m - x, copy_d[k][::-1])
                else:
                    copy_d[k] = copy_d[k][::-1]
            return copy_d

        reversed = map(reverse_data, trn)
        trn += reversed


    if args['sorted']:
        trn = sorted(trn, key=lambda sent: len(sent['cseq']))
        dev = sorted(dev, key=lambda sent: len(sent['cseq']))
        tst = sorted(tst, key=lambda sent: len(sent['cseq']))

    ntrnsent, ndevsent, ntstsent = list(map(len, (trn,dev,tst)))
    logger.info('# of sents trn, dev, tst: {} {} {}'.format(ntrnsent, ndevsent, ntstsent))

    MAX_LENGTH = max(len(sent['cseq']) for sent in chain(trn,dev,tst))
    MIN_LENGTH = min(len(sent['cseq']) for sent in chain(trn,dev,tst))
    AVG_LENGTH = np.mean([len(sent['cseq']) for sent in chain(trn,dev,tst)])
    STD_LENGTH = np.std([len(sent['cseq']) for sent in chain(trn,dev,tst)])
    logger.info('maxlen: {} minlen: {} avglen: {:.2f} stdlen: {:.2f}'.format(MAX_LENGTH, MIN_LENGTH, AVG_LENGTH, STD_LENGTH))

    feat = featchar.Feat(args['feat'])
    feat.fit(trn,dev,tst)

    batcher = Batcher(args['n_batch'], feat)
    reporter = Reporter(feat, rep.get_ts)

    validator = Validator(trn, dev, tst, batcher, reporter) if args['curriculum'] < 2 else Curriculum(trn, dev, batcher, reporter, args['curriculum'])

    # select rnn
    RNN = RDNN

    rnn_params = extract_rnn_params(args)
    rdnn = RNN(feat.NC, feat.NF, args)
    """ tprobs """ # TODO
    tfollow = set()
    for sent in trn:
        tseq = feat.tseqenc.transform([t for t in sent['tseq']])
        tfollow.update(set(zip(tseq,tseq[1:])))
    tprobs = np.zeros((feat.NC,feat.NC),dtype=np.float32)
    for i,j in tfollow:
        tprobs[i,j] = 1
    rdnn.tprobs = tprobs
    """ end tprobs """
    validator.validate(rdnn, args)
    # lr: scipy.stats.expon.rvs(loc=0.0001,scale=0.1,size=100)
    # norm: scipy.stats.expon.rvs(loc=0, scale=5,size=10)

def objective(hargs):
    logger = logging.getLogger()
    
    parser = get_arg_parser()
    args = vars(parser.parse_args())

    args['lr'] = hargs['lr']
    args['opt'] = hargs['opt']
    args['decoder'] = hargs['decoder']
    args['n_hidden'] = hargs['n_hidden']
    args['n_batch'] = hargs['n_batch']
    args['norm'] = hargs['norm']
    args['shuf'] = hargs['shuf']
    args['fbias'] = hargs['fbias']
    args['emb'] = hargs['emb']

    if hargs['layer'] == 1:
        args['drates'] = [hargs['d1'], hargs['d2']]
        args['n_hidden'] = [hargs['n_hidden']]
    elif hargs['layer'] == 2:
        args['drates'] = [hargs['d1'], hargs['d2'], hargs['d3']]
        args['n_hidden'] = [hargs['n_hidden'], hargs['n_hidden']]
    else:
        args['drates'] = [hargs['d1'], hargs['d2'], hargs['d3'], hargs['d4']]
        args['n_hidden'] = [hargs['n_hidden'], hargs['n_hidden'], hargs['n_hidden']]

    # print args
    for k,v in sorted(args.iteritems()):
        logger.info('{}:\t{}'.format(k,v))
    logger.info('{}:\t{}'.format('base_log_name',base_log_name))

    trn, dev, tst = get_sents(args['lang'])

    if args['breaktrn']:
        trn = [subsent for sent in trn for subsent in break2subsents(sent)]

    if args['captrn']:
        trn = filter(lambda sent: len(sent['ws'])<args['captrn'], trn)

    if args['sample']>0:
        trn_size = args['sample']*1000
        trn = sample_sents(trn,trn_size)

    repclass = getattr(rep, 'Rep'+args['rep'])
    repobj = repclass()

    for d in (trn,dev,tst):
        for sent in d:
            if args['tagging'] == 'io':
                sent['ts'] = encoding.any2io(sent['ts'])
            sent.update({
                'cseq': repobj.get_cseq(sent), 
                'wiseq': repobj.get_wiseq(sent), 
                'tseq': repobj.get_tseq(sent)})
    
    if args['reverse']:
        def reverse_data(d):
            copy_d = d.copy()
            
            for k in copy_d.keys():
                if k == "wiseq":
                    m = max(copy_d[k])
                    copy_d[k] = map(lambda x: -1 if x == -1 else m - x, copy_d[k][::-1])
                else:
                    copy_d[k] = copy_d[k][::-1]
            return copy_d

        reversed = map(reverse_data, trn)
        trn += reversed


    if args['sorted']:
        trn = sorted(trn, key=lambda sent: len(sent['cseq']))
        dev = sorted(dev, key=lambda sent: len(sent['cseq']))
        tst = sorted(tst, key=lambda sent: len(sent['cseq']))

    ntrnsent, ndevsent, ntstsent = list(map(len, (trn,dev,tst)))
    logger.info('# of sents trn, dev, tst: {} {} {}'.format(ntrnsent, ndevsent, ntstsent))

    MAX_LENGTH = max(len(sent['cseq']) for sent in chain(trn,dev,tst))
    MIN_LENGTH = min(len(sent['cseq']) for sent in chain(trn,dev,tst))
    AVG_LENGTH = np.mean([len(sent['cseq']) for sent in chain(trn,dev,tst)])
    STD_LENGTH = np.std([len(sent['cseq']) for sent in chain(trn,dev,tst)])
    logger.info('maxlen: {} minlen: {} avglen: {:.2f} stdlen: {:.2f}'.format(MAX_LENGTH, MIN_LENGTH, AVG_LENGTH, STD_LENGTH))

    feat = featchar.Feat(args['feat'])
    feat.fit(trn,dev,tst)

    batcher = Batcher(args['n_batch'], feat)
    reporter = Reporter(feat, rep.get_ts)

    validator = Validator(trn, dev, tst, batcher, reporter) if args['curriculum'] < 2 else Curriculum(trn, dev, batcher, reporter, args['curriculum'])

    # select rnn
    RNN = RDNN

    rnn_params = extract_rnn_params(args)
    rdnn = RNN(feat.NC, feat.NF, args)
    """ tprobs """ # TODO
    tfollow = set()
    for sent in trn:
        tseq = feat.tseqenc.transform([t for t in sent['tseq']])
        tfollow.update(set(zip(tseq,tseq[1:])))
    tprobs = np.zeros((feat.NC,feat.NC),dtype=np.float32)
    for i,j in tfollow:
        tprobs[i,j] = 1
    rdnn.tprobs = tprobs
    """ end tprobs """
    devbest = validator.validate(rdnn, args)

    d = {}
    d['loss'] = 100 - devbest
    d['status'] = STATUS_OK
    return d


if __name__ == '__main__':
    parser = get_arg_parser()
    args = vars(parser.parse_args())
    args['drates'] = args['drates'] if any(args['drates']) else [0]*(len(args['n_hidden'])+1)

    # logger setup
    import socket
    host = socket.gethostname().split('.')[0]
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    shandler = logging.StreamHandler()
    shandler.setLevel(logging.INFO)
    param_log_name = ','.join(['{}:{}'.format(p,args[p]) for p in LPARAMS])
    param_log_name = valid_file_name(param_log_name)
    base_log_name = '{}:{},{}'.format(host, theano.config.device, param_log_name if args['log'] == 'das_auto' else args['log'])
    ihandler = logging.FileHandler('{}/{}.info'.format(LOG_DIR,base_log_name), mode='w')
    ihandler.setLevel(logging.INFO)
    dhandler = logging.FileHandler('{}/{}.debug'.format(LOG_DIR,base_log_name), mode='w')
    dhandler.setLevel(logging.DEBUG)
    logger.addHandler(shandler);logger.addHandler(ihandler);logger.addHandler(dhandler);

    common = {}
    common['lr'] = hp.choice('lr', [0.001, 0.001, 0.005, 0.01, 0.1, 1, 2])
    common['opt'] = hp.choice('opt', ['adam', 'sgd'])
    common['decoder'] = hp.choice('decoder', [0, 1])
    common['n_hidden'] = hp.choice('n_hidden', [64, 128, 256])
    common['n_batch'] = hp.choice('n_batch', [32, 64, 128])
    common['norm'] = hp.choice('norm', [0.01, 0.1, 1, 2, 5, 10])
    common['shuf'] = hp.choice('shuf', [True, False])
    common['fbias'] = hp.uniform('fbias', 0, 2)
    common['emb'] = hp.choice('emb', [0, 64, 128, 256])
    common['d1'] = hp.uniform('d1', 0, 1)
    common['d2'] = hp.uniform('d2', 0, 1)
    common['d3'] = hp.uniform('d3', 0, 1)
    common['d4'] = hp.uniform('d4', 0, 1)
    common['layer'] = hp.choice('layer', [1,2,3])

    space = common
    trials = Trials()
    best = fmin(objective,
        space=space,
        algo=tpe.suggest,
        max_evals=50,
        trials=trials,
        )

    logger.info("Best: {}".format(best))

