#!/usr/bin/env python

"""
    MHC prediction base module for core classes
    Created November 2013
    Copyright (C) Damien Farrell
"""

from __future__ import absolute_import, print_function
import sys, os, shutil, string
import csv, glob, pickle
import time, io
import operator as op
import re, types
import subprocess
from subprocess import CalledProcessError
import numpy as np
import pandas as pd
from Bio.Seq import Seq
from Bio import SeqIO
from Bio.SeqRecord import SeqRecord
from . import utilities, peptides, sequtils, tepitope

home = os.path.expanduser("~")
path = os.path.dirname(os.path.abspath(__file__)) #path to module
datadir = os.path.join(path, 'mhcdata')
predictors = ['tepitope','netmhciipan','iedbmhc1','iedbmhc2','bcell']
iedbmethods = ['arbpython','comblib','consensus3','IEDB_recommended',
               'NetMHCIIpan','nn_align','smm_align','tepitope']
iedbsettings = {'cutoff_type': 'none', 'pred_method': 'IEDB_recommended',
            'output_format': 'ascii', 'sort_output': 'position_in_sequence',
            'sequence_format': 'auto', 'allele': 'HLA-DRB1*01:01', 'length':'11',
            'sequence_file': None}
iedbkeys = {'consensus3': ['Allele','Start','End','Sequence','consensus_percentile',
            'comblib_core','comblib_score','comblib_percentile','smm_core','smm_score',
            'smm_percentile','nn_core','nn_score','nn_percentile','Sturniolo core',
            'Sturniolo score','Sturniolo percentile'],
        'IEDB_recommended': ['Allele','Start','End','Sequence','consensus_percentile',
            'comblib_core','comblib_score','comblib_percentile','smm_core','smm_score',
            'smm_percentile','nn_core','nn_score','nn_percentile','netMHCIIpan_core',
            'netMHCIIpan_score','netMHCIIpan_percentile','Sturniolo core',
            'Sturniolo score','Sturniolo percentile','methods'],
        'NetMHCIIpan': ['Allele','Start','End','Core','Sequence','IC50']}

#these paths should be set by user before calling predictors
iedbmhc1path = '/local/iedbmhc1/'
iedbmhc2path = '/local/iedbmhc2/'
iedbbcellpath = '/local/iedbbcell/'

def first(x):
    return x.iloc[0]

def getIEDBRequest(seq, alleles='HLA-DRB1*01:01', method='consensus3'):
    import requests
    url = 'http://tools.iedb.org/tools_api/mhcii/'
    values = {'method' : method,
              'sequence_text' : seq,
              'allele' : alleles }
    r=requests.post(url,data=values)
    df=pd.read_csv(io.StringIO(r.content),sep='\t')
    #df=df.drop(['nn_align_core','nn_align_ic50','nn_align_rank'])
    return df

def getOverlapping(index, s, length=9, cutoff=25):
    """Get all mutually overlapping kmers within a cutoff area"""
    g=[s]
    vals = [i for i in range(s, s+cutoff) if i in index]
    for i in range(len(vals)-1):
        if vals[i+1]<=vals[i]+length:
            g.append(vals[i+1])
        else:
            break
    return g

def checkMembers(g,clusts):
    """Check if a group intersects any of the current clusters"""
    for i in clusts:
        common = list(set(g) & set(i))
        if len(common)>0 or len(g)<2:
            #print i,common
            return False
    return True

def getClusters(B, clustlen=25, cutoff=0.05):
    """Get clusters of binders from a set of predictions
      df: a pandas dataframe with one set of predictions per row"""

    nmer = len(B.iloc[0].peptide)
    overlap = clustlen - nmer
    #print clustlen, nmer, overlap
    locs = pd.Series(B.peptide.values,index=B.pos).to_dict()
    #ad hoc method to get overlapping epitopes and
    #return largest unique groups as clusters
    groups=[]
    for i in locs:
        g = getOverlapping(locs, int(i), overlap, clustlen)
        groups.append(g)
    ranked = sorted(groups, key=len, reverse=True)
    clusts=[]
    for g in ranked:
        if checkMembers(g, clusts) == True:
            clusts.append(g)
    #print clusts
    return clusts

def dbscan(B=None, x=None, dist=7, minsize=4):
    """Density-Based Spatial clustering. Finds core samples of
      high density and expands clusters from them."""

    from sklearn.cluster import DBSCAN
    if B is not None:
        if len(B)==0:
            return
        x = sorted(B.pos.astype('int'))
    X = np.array(zip(x,np.zeros(len(x))), dtype=np.int)
    db = DBSCAN(eps=dist, min_samples=minsize)
    db.fit(X)
    labels = db.labels_
    n_clusters_ = len(set(labels))
    clusts=[]
    for k in range(n_clusters_):
        my_members = labels == k
        #print "cluster {0}: {1}".format(k, X[my_members, 0])
        if len(X[my_members, 0])>0:
            clusts.append(list(X[my_members, 0]))
    #print clusts
    return clusts

def getPredictor(name='tepitope', **kwargs):
    """Get a predictor"""

    if name == 'netmhciipan':
        return NetMHCIIPanPredictor(**kwargs)
    elif name == 'iedbmhc1':
        return IEDBMHCIPredictor(**kwargs)
    elif name == 'iedbmhc2':
        return IEDBMHCIIPredictor(**kwargs)
    elif name == 'bcell':
        return BCellPredictor(**kwargs)
    elif name == 'tepitope':
        return TEpitopePredictor(**kwargs)
    else:
        print ('no such predictor %s' %name)
        return

def getLength(data):
    """Get peptide length of a dataframe of predictions"""

    if len(data)>0:
        return len(data.head(1).peptide.max())
    return

def createTempSeqfile(sequences, seqfile='tempseq.fa'):
    if isinstance(sequences, str):
        sequences=[sequences]
    out = open(seqfile, 'w')
    i=1
    for seq in sequences:
        SeqIO.write(SeqRecord(Seq(seq),id='temp%s'%i,
                    description='temp'), out, 'fasta')
        i+=1
    out.close()
    return seqfile

def getSequence(seqfile):
    """Get sequence from fasta file"""
    recs = list(SeqIO.parse(seqfile, 'fasta'))[0]
    sequence = recs.seq.tostring()
    return sequence

def getOverlappingBinders(B):
    for df in B:
        df.set_index('peptide',inplace=True)
    print (B)
    x = pd.concat(B,join='inner')
    #x =  x[['pos']].sort('pos')
    return x

def getNearest(df):
    """Get nearest binder"""

    grps = df.groupby('name')
    new = []
    def closest(x,g):
        if len(g.pos)==1:
            return 1
        return min([abs(x.pos-i) for i in g.pos if i!=x.pos])
    for i,g in grps:
        positions = g.pos
        g['nearest'] = g.apply(lambda x: closest(x,g),axis=1)
        new.append(g)
    df = pd.concat(new)
    return df

'''def getBinders(preds,n=3):
    """Get binders for multiple predictors"""

    b={}
    for m in preds:
        pred = preds[m]
        binders = pred.getPromiscuousBinders(n=n)
        if len(binders)>0:
            binders = binders.sort('pos')
            b[m] = binders
    return b'''

def getBindersfromPath(method, path, n=3, cutoff=0.95, promiscuous=True):
    """
    Get all binders from a set of binding results stored in a directory.

    Args:
        path: The file path with all the binding prediction results
        method: Prediction method
        n: minimum number of alleles if using promiscuous binders
        promiscuous: whether to return only promiscuous binders

    Returns:
        A dataframe with all binders matching the required critieria
    """

    print ('getting binders..')
    binders = []
    #m=method
    #if m=='bcell': return #not applicable
    l=9
    P = getPredictor(method)
    files = glob.glob(os.path.join(path, '*.csv'))
    #get allele specific cutoffs
    P.allelecutoffs = getCutoffs(P, path, cutoff, overwrite=True)
    key = P.scorekey
    for f in files:
        #df = pd.read_msgpack(f)
        df = pd.read_csv(f, index_col=0)
        if not key in df.columns:
            continue
        #print (df[:3])
        if promiscuous== True:
            b = P.getPromiscuousBinders(data=df,n=n)
        else:
            b = P.getBinders(data=df)
        #print b[:5]
        binders.append(b)
    result = pd.concat(binders)
    result['start'] = result.pos
    result['end'] = result.pos+result.peptide.str.len()
    return result

def getCutoffs(predictor, path, data=None, q=0.98, overwrite=False):
    """
    Get global cutoffs for predictions in path
    Args:
        path: The file path with all the binding prediction results
        method: Prediction method
        q: percentile level of score to select cutoffs
    Returns:
        A dictionary with cutoff values per allele
    """

    quantfile = os.path.join(path,'quantiles.csv')
    if not os.path.exists(quantfile) or overwrite==True:
        getScoreDistributions(predictor, path)
    quantiles = pd.read_csv(quantfile,index_col=0)
    cutoffs = dict(quantiles.ix[q])
    return cutoffs

def getScoreDistributions(predictor, path):
    """Get global score distributions and save quantile values for each allele
       Assumes all the files in path represent related proteins"""

    files = glob.glob(os.path.join(path, '*.csv'))
    results = []
    P = predictor
    key = P.scorekey
    for f in files[:200]:
        #df = pd.read_msgpack(f)
        df = pd.read_csv(f, index_col=0)
        if not key in df.columns:
            continue
        #print (df[:3])
        #print (df.info())
        x = df.pivot_table(index='peptide', columns='allele', values=key)
        #print (x)
        #print x[:5]
        results.append(x)
    result = pd.concat(results)
    percs = np.arange(0.01,1,0.01)
    bins = result.quantile(percs)
    #reverse is best values are lower
    if P.operator == '<':
        bins.index = pd.Series(bins.index).apply(lambda x: 1-x)
    outfile = os.path.join(path,'quantiles.csv')
    print (outfile)
    bins.to_csv(outfile,float_format='%.3f')
    df= pd.read_csv(outfile,index_col=0)
    print (df.ix[0.96])
    return

def getStandardMHCI(name):
    """Taken from iedb mhc1 utils.py"""
    temp = name.strip().split('-')
    length = temp[-1]
    mhc = '-'.join(temp[0:-1])
    return mhc

def getDRBList(a):
    """Get DRB list in standard format"""
    s = pd.Series(a)
    s = s[s.str.contains('DRB')]
    s = s.apply(lambda x:'HLA-'+x.replace('_','*'))
    return list(s)

def getDQPList(a):
    """Get DRB list in standard format"""
    s = pd.Series(a)
    s = s[s.str.contains('DQ')]
    s = s.apply(lambda x:x.replace('_','*'))
    return list(s)

def getStandardMHCII(x):
    return 'HLA'+x.replace('_','*')

class Predictor(object):
    """Base class to handle generic predictor methods, usually these will
       wrap methods from other modules and/or call command line predictors.
       Subclass for specific functionality"""

    def __init__(self, data=None):
        self.data = data
        self.name = ''
        self.scorekey = 'score'
        self.operator = '<'
        self.rankascending = 1
        #can specify per allele cutoffs here
        self.allelecutoffs = {}
        return

    def __repr__(self):

        if (self.data is None) or len(self.data) == 0:
            return '%s predictor' %self.name
        else:
            n = len(self.data.name.unique())
            return '%s predictor with results in %s proteins' %(self.name, n)

    def predict(self, sequence, peptide):
        """Does the actual scoring. Must override this.
           Should return a pandas DataFrame"""
        return

    def prepareData(self, result, name, allele):
        """Put raw prediction data into DataFrame and rank,
           override for custom processing"""

        df = pd.DataFrame(result, columns=['peptide','core','pos','score'])
        df['name'] = name
        df['allele'] = allele
        self.getRanking(df)
        return df

    def getRanking(self, df):
        """Add a ranking column according to scorekey"""

        s=self.scorekey
        df['rank'] = df[s].rank(method='min',ascending=self.rankascending)
        df.sort_values(by=['rank','name','allele'], ascending=True, inplace=True)
        return

    def evaluate(self, df, key, value, operator='<'):
        """Evaluate binders less than or greater than value - the cutoff"""

        if operator == '<':
            return df[df[key] <= value]
        else:
            return df[df[key] >= value]

    def getBinders(self, method='cutoff', q=0.01, data=None, name=None):
        """
        Get the top scoring percentile or using cutoff.
        Args:
            data: binding predictions for one or more proteins
            q: quantile threshold for selecting global cutoffs
        Returns:
            pandas DataFrame of all binders
        """

        if data is None:
            if self.data is None:
                print ('no prediction data available')
                return
            else:
                data = self.data
        if name != None:
            data = data[data.name==name]

        key = self.scorekey
        op = self.operator
        if method == 'cutoff':
            #this allows us to use global allele based cutoffs
            #must be set first as an attribute
            res = []
            for a,g in data.groupby('allele'):
                if a in self.allelecutoffs:
                    cutoff = self.allelecutoffs[a]
                else:
                    cutoff = self.cutoff
                b = self.evaluate(g, key, cutoff, op)
                res.append(b)
            return pd.concat(res)
        elif method == 'rank':
            #get top ranked % per protein
            res=[]
            for i,g in data.groupby('name'):
                value = g['rank'].quantile(q=q)
                b = g[g['rank'] <= value]
                res.append(b)
            return pd.concat(res)

    def getPromiscuousBinders(self, n=3, method='cutoff', data=None, name=None):
        """
        Get top scoring binders present in at least n alleles.
        Args:
            n: number of alleles
            method: method to use for
            data: a dataframe of prediction data, optional
            name: name of the proteins to use, optional
        Returns:
            pandas DataFrame with binders
        """

        if data is None:
            data = self.data
        #get binders using the provided or current prediction data
        b = self.getBinders(method, data=data, name=name)

        if b is None or len(b) == 0:
            return pd.DataFrame()
        grps = b.groupby(['peptide','pos','name'])
        if self.operator == '<':
            func = min
        else:
            func = max
        s = grps.agg({'allele':pd.Series.count,self.scorekey:func})
        s = s[s.allele>=n]
        s = s.reset_index()
        #merge frequent binders with original data to retain fields
        p = list(data.groupby('allele'))[0][1]
        p = p.drop(['allele','rank',self.scorekey],1)

        if not s.empty:
            final = pd.merge(p,s,how='right',on=['peptide','pos','name'])
            l = getLength(b)
            #if l > 9:
            g = final.groupby('core')
            final = g.agg({self.scorekey:max,'name':first,'peptide':first,'pos':first,
                            'allele':first})
            final = final.reset_index().sort_values('pos')
            #print merged.sort('pos')
            #print final
            return final
        else:
            return pd.DataFrame()

    def getUniqueCores(self, binders=False):
        """Get only unique cores"""

        if binders == True:
            df = self.getBinders()
        else:
            df = self.data
        grouped = df.groupby('core')
        cores = grouped.agg({self.scorekey:max})
        #cores = df.loc[grouped[self.scorekey].max().index]
        cores.sort(self.scorekey, inplace=True, ascending=self.rankascending)
        #print cores
        return cores

    '''def predictSequences(self, data, seqkey='peptide', length=11,
                        alleles=[], save=False):
        results=[]
        for i,row in data.iterrows():
            seq = row[seqkey]
            if len(seq)<=length: continue
            #print (i,seq)
            res=[]
            for a in alleles:
               df = self.predict(sequence=seq,length=length,
                                    allele=a,name=i)
               res.append(df)
            res = pd.concat(res)
            results.append(res)
            #if save==True:
            #    pd.to_msgpack('predictions_%s.mpk' %self.name, res, append=True)
        self.data = pd.concat(results)
        return results'''

    def predictProteins(self, recs, length=11, names=None,
                         alleles=[], path=None):
        """Get predictions for a set of proteins and/or over multiple alleles
          Args:
            recs: protein sequences in a pandas DataFrame
            length: length of peptides to predict
            names: names of proteins to use from sequences
            alleles: allele list
            path: if results are to be saved to disk provide a path, otherwise results
            for all proteins are stored in the data attribute of the predictor
          Returns:
            a dataframe of predictions over multiple proteins"""

        if type(alleles) is str:
            alleles = [alleles]
        elif type(alleles) is pd.Series:
            alleles = alleles.tolist()
        if len(alleles) == 0:
            return
        self.length = length
        recs = sequtils.getCDS(recs)
        if names != None:
            recs = recs[recs.locus_tag.isin(names)]
        proteins = list(recs.iterrows())
        results = []
        for i,row in proteins:
            st = time.time()
            seq = row['translation']
            name = row['locus_tag']
            #print i,name
            res = []
            for a in alleles:
                df = self.predict(sequence=seq,length=length,
                                    allele=a,name=name)
                if df is not None:
                    res.append(df)
            res = pd.concat(res)
            if path is not None and path != '':
                if not os.path.exists(path):
                    os.mkdir(path)
                #fname = os.path.join(path, name+'.mpk')
                #pd.to_msgpack(fname, res)
                fname = os.path.join(path, name+'.csv')
                res.to_csv(fname)
            else:
                results.append(res)
        print ('predictions done for %s proteins in %s alleles' %(len(proteins),len(alleles)))
        if path is None:
            #if no path we keep assign results to the data object
            #assumes we have enough memory
            self.data = pd.concat(results)
        else:
            print ('results saved to %s' %os.path.abspath(path))
        return

    def load(self, filename):
        """Load results for one or more proteins"""

        self.data = pd.read_csv(filename, index_col=0)
        return

    def save(self, label, singlefile=True):
        """Save all current predictions dataframe with some metadata"""

        if singlefile == True:
            fname = 'epit_%s_%s_%s.mpk' %(label,self.name,self.length)
            print ('saving as %s' %fname)
            meta = {'method':self.name, 'length':self.length}
            pd.to_msgpack(fname, meta)
            for i,g in self.data.groupby('name'):
                pd.to_msgpack(fname, g, append=True)
        else:
            #save one file per protein/name
            path = os.path.join(label,self.name)
            print ('saving to %s' %path)
            if not os.path.exists(path):
                os.makedirs(path)
            for name,df in self.data.groupby('name'):
                outfile = os.path.join(path, name+'.csv')
                #pd.to_msgpack(outfile,df)
                df.to_csv(outfile)
        return

    def summary(self):
        '''print 'high binders: %s' %len(self.getBinders())
        print 'binders with unique cores: %s' %len(self.getUniqueCores(binders=True))
        allelegrps = self.data.groupby('allele')
        print '%s peptides in %s proteins and %s alleles' %(len(self.data),
                                            len(proteins),len(allelegrps))'''
        return

    def reshape(self, name=None):
        """Return pivoted data over alleles for summary use"""

        df = self.data
        if name != None:
            df = df[df.name==name]
        p = df.pivot_table(index='peptide', columns='allele', values=self.scorekey)
        p = p.reset_index()
        x = list(df.groupby('allele'))[0][1]
        p = p.merge(x[['pos','peptide']],on='peptide')
        p['mean'] = p.mean(1)
        p=p.sort('mean',ascending=self.rankascending)
        return p

    def getNames(self):
        grp = self.data.groupby('name')
        return sorted(dict(list(grp)).keys())

    def benchmark(self):
        """Benchmark on known cores"""
        hits=0; total=0
        templates = Threading.templates
        for allele,row in templates.iterrows():
            name = row['pdbid']
            nativecore = row['core']
            seq = row['peptide']
            if len(seq)<=11: continue
            df = self.predict(seq,allele=allele,length=9)
            if len(df)==0: continue
            rank = df[df.peptide==nativecore]['rank'].values[0]
            #print df
            print (allele,df.iloc[0].peptide,nativecore,rank,df.iloc[0][self.scorekey])
            if rank==1:
                hits+=1
            total+=1
        print ('%s/%s correct' %(hits,total))
        return

    def benchmarkKnownAntigens(self, expdata=None):
        """Test ability to rank known epitiopes/binders in antigen sequences"""

        import pylab as plt
        if expdata==None:
            #expdata = pd.read_csv(os.path.join(datadir,'expdata/bovine_responder_jones.csv'))
            expdata = pd.read_csv(os.path.join(datadir,'expdata/SYF.csv'))
        R=[]
        for name,row in expdata.dropna().iterrows():
            true = row['peptide']
            protseq = row['sequence']
            if protseq == np.NaN or len(true)<15 or len(true)>20:
                continue
            a = row['allele']
            if a == 'bola':
                #a='BoLA-DRB3*1601'
                a='HLA-DRB1*0101'
            df = self.predict(protseq,allele=a,length=9,name=true)
            if len(df)==0: continue
            print (a,true)
            self.cutoff=0
            b = self.getBinders()
            #print b
            def instring(x):
                if x in true: return True
                else: return False
            found = df[df.peptide.apply(instring)]
            #print found
            #if len(found)==0:
            #    continue
            toprank = found['rank'].values[0]
            #meanrank = np.mean(found['score'].values[:5])
            sc = found[self.scorekey].max()
            #locs = b['pos'].values
            #print getClusters(b)
            bnd = len(b[b.peptide.apply(instring)])
            freq = 0#row['freq']
            #rank = df[df.peptide==true]['rank'].values[0]
            R.append([a, true, toprank, sc, bnd, freq, len(protseq)])
            #plotPerAllele(self, path='benchmark')
        R=pd.DataFrame(R,columns=['allele','peptide','toprank','score','binders','freq','length'])
        R['percentile'] = R.toprank/R.length*100
        #print R
        R.hist('percentile',by=R.allele)
        x=R.groupby('allele').agg({'percentile':np.mean,'score':np.mean})
        x.plot(kind='bar',subplots=True)
        #print x
        '''fig=plt.figure(figsize=(10,10))
        ax=fig.add_subplot(221)
        R.plot(x='toprank',y='freq',kind='scatter',ax=ax,alpha=0.6)
        ax=fig.add_subplot(222)
        R.plot(x='meansc',y='freq',kind='scatter',ax=ax,alpha=0.6)
        ax=fig.add_subplot(223)
        R.plot(x='binders',y='freq',kind='scatter',ax=ax,alpha=0.6)'''
        plt.tight_layout()
        plt.show()
        return

    def plotBinders(self, name, cldist=7, n=2, tmregions=None,
                    legend=False, figsize=(9,3), ax=None):
        """Plot binders as bars per allele"""

        fig=None
        if ax==None:
            fig=plt.figure(figsize=figsize)
            ax=fig.add_subplot(111)
        cmap = plt.cm.get_cmap('jet')
        sckey = self.scorekey
        df = self.data[self.data.name==name]
        if self.cutoff < 0:
            highest = min(df[sckey])
        else:
            highest = max(df[sckey])
        if len(self.allelecutoffs)>0:
            lowest = min(self.allelecutoffs.values())
        else:
            lowest = self.cutoff
        lims = (lowest,highest)
        pb = self.getPromiscuousBinders(data=df,n=n)
        #pball = self.getPromiscuousBinders(data=df,n=1)
        grps = df.groupby('allele')
        cl = dbscan(pb,dist=cldist)
        j=0
        labels = []
        leg = []
        if len(pb)>0:
            for a, df in grps:
                c = cmap(float(j)/(len(grps)))
                b = self.getBinders(data=df)
                ind = np.arange(len(df))
                b = b[b.pos.isin(pb.pos)] #show only promiscuous
                b.sort('pos',inplace=True)
                y = b[sckey].values
                x = b['pos'].values
                bars = plotBars(x,y,ind,color=c,ax=ax,label='')
                labels.extend(zip(bars,b.peptide))
                if len(bars)>0:
                    leg.append((a,bars[0]))
                j+=1
        ax.set_title(self.name+' '+name)
        ax.set_ylim(lims)
        plt.setp(ax.get_xticklabels(), visible=True)

        #moving average plot of epitope density
        #df=df.sort('pos')
        #m = df[sckey].where(df[sckey].isin(pb.pos),other=0)
        #y = m.apply(lambda x: pball.sco
        #y = pd.stats.moments.rolling_mean(m, 10)
        #ax2 = ax.twinx()
        #ax2.plot(df.pos.values, y, '-',lw=2)

        plotRegions(cl,ax,alpha=0.2,ls='dashed')
        if tmregions != None:
             plotRegions(tmregions,ax,color='y',alpha=0.2,ls='dashed')
        if legend == True and len(leg)>0:
            patches,l = zip(*leg)
            ax.legend(l,patches,fontsize=9,mode="expand",ncol=6,framealpha=0.5)
        plt.tight_layout()
        return fig, labels

class NetMHCIIPanPredictor(Predictor):
    """netMHCIIpan predictor"""
    def __init__(self, data=None):
        Predictor.__init__(self, data=data)
        self.name = 'netmhciipan'
        self.colnames = ['pos','HLA','peptide','Identity','Pos','Core',
                         '1-log50k(aff)','Affinity','Rank']
        self.scorekey = 'Affinity' #'1-log50k(aff)'
        self.cutoff = 500
        self.operator = '<'
        self.rankascending = 1

    def readResult(self, res):
        """Read raw results from netMHCIIpan output"""

        data=[]
        res = res.split('\n')[19:]
        ignore=['Protein','pos','']
        for r in res:
            if r.startswith('-'): continue
            row = re.split('\s*',r.strip())[:9]
            if len(row)!=9 or row[0] in ignore:
                continue
            data.append(dict(zip(self.colnames,row)))
        return data

    def prepareData(self, df, name):

        df = df.convert_objects(convert_numeric=True)
        #df = df.apply(pd.to_numeric)#, errors='ignore')
        df['name'] = name
        df.rename(columns={'Core': 'core','HLA':'allele'}, inplace=True)
        df=df.drop(['Pos','Identity','Rank'],1)
        df=df.dropna()
        self.getRanking(df)
        self.data = df
        return

    def runSequence(self, seq, length, allele):
        """Run netmhciipan for a single sequence"""

        seqfile = createTempSeqfile(seq)
        cmd = 'netMHCIIpan -s -length %s -a %s -f %s' %(length, allele, seqfile)
        #print cmd
        temp = subprocess.check_output(cmd, shell=True, executable='/bin/bash')
        rows = self.readResult(temp)
        df = pd.DataFrame(rows)
        return df

    def predict(self, sequence=None, peptides=None, length=11,
                    allele='HLA-DRB1*0101', name='',
                    pseudosequence=None):
        """Call netMHCIIpan command line"""

        #assume allele names are in standard format HLA-DRB1*0101
        try:
            allele = allele.split('-')[1].replace('*','_')
        except:
            print('invalid allele')
            return
        if peptides != None:
            res = pd.DataFrame()
            for p in peptides:
                temp = self.runSequence(p, len(p), allele)
                res = res.append(temp,ignore_index=True)
        else:
            res = self.runSequence(sequence, length, allele)
        if len(res)==0:
            return res
        self.prepareData(res, name)
        #print self.data[self.data.columns[:7]][:5]
        return self.data

    def getAlleleList(self):
        """Get available alleles"""

        cmd = 'netMHCIIpan -list'
        try:
            temp = subprocess.check_output(cmd, shell=True, executable='/bin/bash')
        except:
            print('netmhciipan not installed?')
            return []
        alleles=temp.split('\n')[34:]
        #print sorted(list(set([getStandardmhc1Name(i) for i in alleles])))
        return alleles

class IEDBMHCIPredictor(Predictor):
    """Using IEDB tools method, requires iedb-mhc1 tools"""
    def __init__(self, data=None):
        Predictor.__init__(self, data=data)
        self.name = 'iedbmhc1'
        self.scorekey = 'ic50'
        self.methods = {'ANN':'ann_ic50','IEDB_recommended':'smm_ic50',
                         'Consensus (ANN,SMM)':'ann_ic50','NetMHCpan':'netmhcpan_ic50'}
        self.cutoff = 500
        self.operator = '<'
        self.rankascending = 1
        self.iedbmethod = 'IEDB_recommended'
        #self.path = iedbmhc1path

    def predict(self, sequence=None, peptides=None, length=11,
                   allele='HLA-A*01:01', name=''):
        """Use iedb MHCII python module to get predictions.
           Requires that the iedb MHC tools are installed locally"""

        seqfile = createTempSeqfile(sequence)
        path = iedbmhc1path
        if not os.path.exists(path):
            print ('iedb mhcI tools not found')
            return
        cmd = os.path.join(path,'src/predict_binding.py')
        cmd = cmd+' %s %s %s %s' %(self.iedbmethod,allele,length,seqfile)
        try:
            temp = subprocess.check_output(cmd, shell=True, executable='/bin/bash',
                stderr=subprocess.STDOUT)
        except CalledProcessError as e:
            print (e)
            return
        self.prepareData(temp, name)
        return self.data

    def prepareData(self, rows, name):
        """Prepare data from results"""

        df = pd.read_csv(io.BytesIO(rows),sep="\t")
        if len(df)==0:
            return
        df = df.replace('-',np.nan)
        df = df.dropna(axis=1,how='all')
        df.reset_index(inplace=True)
        df.rename(columns={'index':'pos',
                           'percentile_rank':'method',
                           'method':'percentile_rank'},
                           inplace=True)
        df['core'] = df.peptide
        df['name'] = name
        key = self.getScoreKey(df)
        df['ic50'] = df[key]
        self.data = df
        self.getRanking(df)
        self.data = df
        return

    def getScoreKey(self, data):
        """Get iedbmhc1 score key from data"""

        m = data['method'].head(1).squeeze()
        key = self.methods[m]
        return key

    def getMHCIList(self):
        """Get available alleles from model_list file and
            convert to standard names"""

        afile = os.path.join(iedbmhc1path, 'data/MHCI_mhcibinding20130222/consensus/model_list.txt')
        df = pd.read_csv(afile,sep='\t',names=['name','x'])
        alleles = list(df['name'])
        alleles = sorted(list(set([getStandardMHCI(i) for i in alleles])))
        return alleles

class IEDBMHCIIPredictor(Predictor):
    """Using IEDB mhcii method, requires iedb-mhc2 tools"""

    def __init__(self, data=None):
        Predictor.__init__(self, data=data)
        self.name = 'iedbmhc2'
        self.scorekey = 'consensus_percentile'
        self.cutoff = 3
        self.operator = '<'
        self.rankascending = 1
        self.methods = ['arbpython','comblib','consensus3','IEDB_recommended',
                    'NetMHCIIpan','nn_align','smm_align','tepitope']
        #self.path = '/local/iedbmhc2/'

    def prepareData(self, rows, name):
        df = pd.read_csv(io.StringIO(rows),delimiter=r"\t")
        extracols = ['Start','End','comblib_percentile','smm_percentile','nn_percentile',
                'Sturniolo core',' Sturniolo score',' Sturniolo percentile']
        df = df.drop(extracols,1)
        df.reset_index(inplace=True)
        df.rename(columns={'index':'pos','Sequence': 'peptide','Allele':'allele'},
                           inplace=True)
        df['core'] = df.nn_core
        df['name'] = name
        self.getRanking(df)
        self.data = df
        return

    def predict(self, sequence=None, peptides=None, length=15,
                   allele='HLA-DRB1*01:01', method='consensus3', name=''):
        """Use iedb MHCII python module to get predictions.
           Requires that the iedb MHC tools are installed locally"""

        seqfile = createTempSeqfile(sequence)
        path = iedbmhc2path
        if not os.path.exists(path):
            print ('iedb mhcII tools not found')
            return
        cmd = os.path.join(path,'mhc_II_binding.py')
        cmd = cmd+' %s %s %s' %(method,allele,seqfile)
        try:
            temp = subprocess.check_output(cmd, shell=True, executable='/bin/bash')
        except:
            print ('allele %s not available?' %allele)
            return
        self.prepareData(temp, name)
        #print self.data
        return self.data

class TEpitopePredictor(Predictor):
    """Predictor using tepitope QM method"""
    def __init__(self, data=None):
        Predictor.__init__(self, data=data)
        self.name = 'tepitope'
        self.pssms = tepitope.getPSSMs()
        self.cutoff = 2
        self.operator = '>'
        self.rankascending = 0

    def predict(self, sequence=None, peptides=None, length=9,
                    allele='HLA-DRB1*0101', name='',
                    pseudosequence=None):

        self.sequence = sequence
        if not allele in self.pssms:
            #print 'computing virtual matrix for %s' %allele
            #try:
            m = tepitope.createVirtualPSSM(allele)
            if m is None:
                return pd.DataFrame()
        else:
            m = self.pssms[allele]
        m = m.transpose().to_dict()
        result = tepitope.getScores(m, sequence, peptides, length)
        df = self.prepareData(result, name, allele)
        self.data = df
        #print df[:12]
        return df

class BCellPredictor(Predictor):
    """Using IEDB tools methods, requires iedb bcell tools.
       see http://tools.immuneepitope.org/bcell """

    def __init__(self, data=None):
        Predictor.__init__(self, data=data)
        self.name = 'iedbmhc1'
        self.scorekey = 'Score'
        self.methods = ['Chou-Fasman', 'Emini', 'Karplus-Schulz',
                        'Kolaskar-Tongaonkar', 'Parker', 'Bepipred']
        self.cutoff = 0.9
        self.operator = '>'
        self.rankascending = 0
        self.iedbmethod = 'Bepipred'
        self.path = iedbbcellpath

    def predict(self, sequence=None, peptides=None, window=None, name=''):
        """Uses code from iedb predict_binding.py """

        value = self.iedbmethod
        currpath=os.getcwd()
        os.chdir(self.path)
        sys.path.append(self.path)
        from src.BCell import BCell
        bcell = BCell()
        filepath = os.path.join(self.path,'bcell_scales.pickle')
        picklefile = open(filepath, 'rb')
        scale_dict = pickle.load(picklefile)
        bcell.scale_dict = scale_dict[value]
        if window==None:
            window = bcell.window
        center = "%d" %round(int(window)/2.0)
        if value == 'Emini':
            results = bcell.emini_method(value, sequence, window, center)
        elif value == 'Karplus-Schulz':
            results = bcell.karplusshulz_method(value, sequence, window, center)
        elif value == 'Kolaskar-Tongaonkar':
            results = bcell.kolaskartongaonkar_method(value, sequence, window, center)
        elif value == 'Bepipred':
            results = bcell.bepipred_method(value, sequence, window, center)
        else:
            results = bcell.classical_method(value, sequence, window, center)

        threshold = round(results[1][0], 3)
        temp=results[0]
        self.prepareData(temp, name)
        os.chdir(currpath)
        return self.data

    def prepareData(self, temp, name):

        df = pd.read_csv(temp,sep=",")
        if len(df)==0:
            return
        #df = df.replace('-',np.nan)
        df = df.dropna(axis=1,how='all')
        #df.reset_index(inplace=True)
        df['name'] = name
        self.data = df
        #print (df)
        return

    def predictProteins(self, recs, names=None, save=False,
                        label='', path='', **kwargs):
        """Get predictions for a set of proteins - no alleles so we override
        the base method for this too. """

        recs = sequtils.getCDS(recs)
        if names != None:
            recs = recs[recs.locus_tag.isin(names)]
        proteins = list(recs.iterrows())
        for i,row in proteins:
            seq = row['translation']
            name = row['locus_tag']
            #print (name)
            res = self.predict(sequence=seq,name=name)
            if save == True:
                #fname = os.path.join(path, name+'.mpk')
                #pd.to_msgpack(fname, res)
                fname = os.path.join(path, name+'.csv')
                res.to_csv(fname)

        return
