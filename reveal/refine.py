from utils import *
from extract import extract
from rem import align,prune_nodes
import bubbles
import schemes
from multiprocessing.pool import Pool
import signal

def refine_bubble_cmd(args):
    if len(args.graph)<1:
        logging.fatal("Specify a gfa file for which bubbles should be realigned.")
        return
    
    # G=nx.MultiDiGraph() #TODO: make sure that refine can handle structural variant edges, so make sure we use a MultiDiGraph here!
    G=nx.DiGraph()
    read_gfa(args.graph[0],None,"",G)
    
    if (args.source==None and args.sink==None) and (args.all or args.complex or args.simple):
        G=refine_all(G,
                            minlength=args.minlength,
                            minn=args.minn,
                            wscore=args.wscore,
                            wpen=args.wpen,
                            maxsize=args.maxsize,
                            minsize=args.minsize,
                            maxcumsize=args.maxcumsize,
                            mincumsize=args.mincumsize,
                            seedsize=args.seedsize,
                            maxmums=args.maxmums,
                            gcmodel=args.gcmodel,
                            complex=args.complex,
                            simple=args.simple,
                            all=args.all,
                            method=args.method,
                            parameters=args.parameters,
                            minconf=args.minconf,
                            nproc=args.nproc,
                            sa64=args.sa64)
    else:
        if args.source==None or args.sink==None:
            logging.error("Specify source sink pair")
            sys.exit(1)

        b=bubbles.Bubble(G,args.source,args.sink)

        nn=max([node for node in G.nodes() if type(node)==int])+1

        bnodes=list(set(b.nodes)-set([b.source,b.sink]))
        sg=G.subgraph(bnodes)

        offsets=dict()
        for sid in G.node[b.source]['offsets']:
            offsets[sid]=G.node[b.source]['offsets'][sid]+len(G.node[b.source]['seq'])

        sourcesamples=set(G.node[b.source]['offsets'].keys())
        sinksamples=set(G.node[b.sink]['offsets'].keys())
        paths=sourcesamples.intersection(sinksamples)

        G.node[b.source]['aligned']=1
        G.node[b.sink]['aligned']=1

        res=refine_bubble(sg,b,offsets,paths,
                                minlength=args.minlength,
                                minn=args.minn,
                                wscore=args.wscore,
                                wpen=args.wpen,
                                maxsize=args.maxsize,
                                seedsize=args.seedsize,
                                maxmums=args.maxmums,
                                gcmodel=args.gcmodel,
                                method=args.method,
                                parameters=args.parameters,
                                minconf=args.minconf,
                                sa64=args.sa64)

        if res!=None:
            ng,path2start,path2end=res
            G,nn=replace_bubble(G,b,ng,path2start,path2end,nn)

    if args.outfile==None:
        fn=args.graph[0].replace(".gfa",".realigned.gfa")
    else:
        fn=args.outfile
    
    # write_gml(G,"")
    # for node in G:
    #     if len(G.node[node]['offsets'])>1:
    #         if 'aligned' not in G.node[node]:
    #             print node,"No attribute!"
    #             G.node[node]['aligned']=1
    #         elif G.node[node]['aligned']==0:
    #             print node,"Attribute not set!"
    #             G.node[node]['aligned']=1

    prune_nodes(G)

    write_gfa(G,"",outputfile=fn)

def replace_bubble(G,bubble,ng,path2start,path2end,nn):
    
    assert(nn not in G)

    bubblenodes=bubble.nodes[1:-1]
    
    for node in bubblenodes: #remove all bubblenodes from the original graph
        G.remove_node(node)

    mapping={}
    for node in ng.nodes(): #add nodes from newly aligned graph to original graph
        mapping[node]=nn
        nn+=1
    
    ng=nx.relabel_nodes(ng,mapping)

    for node,data in ng.nodes(data=True): #add nodes from newly aligned graph to original graph
        G.add_node(node,**data)

    for edge in ng.edges(data=True):
        G.add_edge(edge[0],edge[1],**edge[2])

    for sid in path2start:
        startnode=mapping[path2start[sid][0]]
        if G.has_edge(bubble.source,startnode):
            G[bubble.source][startnode]['paths'].add(sid)
        else:
            G.add_edge(bubble.source,startnode,ofrom='+',oto='+',paths=set([sid]))

    for sid in path2end:
        endnode=mapping[path2end[sid][0]]
        if G.has_edge(endnode,bubble.sink):
            G[endnode][bubble.sink]['paths'].add(sid)
        else:
            G.add_edge(endnode,bubble.sink,ofrom='+',oto='+',paths=set([sid]))

    #Just one possible path from source to start, contract nodes
    if len(G.out_edges(bubble.source))==1 and type(bubble.source)!=str:
        # assert(len(set(path2start.values()))==1)
        startnode=mapping[path2start.values()[0][0]]
        G.node[bubble.source]['seq']+=G.node[startnode]['seq']
        for to in G[startnode]:
            d=G[startnode][to]
            G.add_edge(bubble.source,to,**d)
        G.remove_node(startnode)

    #Just one possible path from end to sink, contract nodes
    if len(G.in_edges(bubble.sink))==1 and type(bubble.sink)!=str:
        # assert(len(set(path2end.values()))==1)
        endnode=mapping[path2end.values()[0][0]]
        G.node[bubble.sink]['seq']=G.node[endnode]['seq']+G.node[bubble.sink]['seq']
        G.node[bubble.sink]['offsets']=G.node[endnode]['offsets']
        for e0,e1,d in G.in_edges(endnode,data=True):
            G.add_edge(e0,bubble.sink,**d)
        G.remove_node(endnode)

    return G,nn

def refine_bubble(sg,bubble,offsets,paths,**kwargs):

    source=bubble.source
    sink=bubble.sink

    logging.info("Realigning bubble between <%s> and <%s>, with %s (cum. size %dbp, in nodes=%d)."%(bubble.source,bubble.sink,kwargs['method'],bubble.cumsize,len(bubble.nodes)-2))

    # if bubble.maxsize>kwargs['maxsize']:
    #     logging.fatal("Bubble (%s,%s) size=%d is too big. Increase --maxsize (if possible), now %d."%(source,sink,bubble.maxsize,kwargs['maxsize']))
    #     return

    if len(bubble.nodes)==3:
        logging.fatal("Indel bubble, no point realigning.")
        return

    #TODO: if bubble contains structural variant edge, track these or simply refuse realignment!

    d={}
    aobjs=[]

    #extract all paths
    for sid in paths:
        seq=extract(sg,sg.graph['id2path'][sid])
        if len(seq)>0:
            aobjs.append((sg.graph['id2path'][sid],seq))

    for name,seq in aobjs:
        logging.debug("IN %s: %s%s"%(name.rjust(4,' '),seq[:200],'...'if len(seq)>200 else ''))

    if kwargs['method']!="reveal": #use custom multiple sequence aligner to refine bubble structure
        ng=msa2graph(aobjs,msa=kwargs['method'],minconf=kwargs['minconf'],parameters=kwargs['parameters'])
        if ng==None:
            logging.fatal("MSA using %s for bubble: %s - %s failed."%(kwargs['method'],source,sink))
            return

    else: #use reveal with different settings
        ng,idx=align(aobjs, minlength=kwargs['minlength'],
                            minn=kwargs['minn'],
                            seedsize=kwargs['seedsize'],
                            maxmums=kwargs['maxmums'],
                            wpen=kwargs['wpen'],
                            wscore=kwargs['wscore'],
                            gcmodel=kwargs['gcmodel'],
                            sa64=kwargs['sa64'])
        T=idx.T
        seq2node(ng,T) #transfer sequence to node attributes

    #map edge atts back to original graph
    for n1,n2,data in ng.edges(data=True):
        old=data['paths']
        new=set()
        for sid in old:
            new.add( sg.graph['path2id'][ng.graph['id2path'][sid]] )
        data['paths']=new

    #map node atts back to original graph
    for node,data in ng.nodes(data=True):
        old=data['offsets']
        new=dict()
        for sid in old:
            new[sg.graph['path2id'][ng.graph['id2path'][sid]]]=old[sid]
        data['offsets']=new

    ng.graph['paths']=sg.graph['paths']
    ng.graph['path2id']=sg.graph['path2id']
    ng.graph['id2path']=sg.graph['id2path']

    mapping={}
    
    path2start=dict()
    path2end=dict()

    #map nodes back to original offsets and idspace, and determine first/last node for every path
    for node,data in ng.nodes(data=True):
        for sid in data['offsets']:
            if sid not in path2start or data['offsets'][sid]<path2start[sid][1]:
                path2start[sid]=(node,data['offsets'][sid])

        for sid in data['offsets']:
            if sid not in path2end or data['offsets'][sid]>path2end[sid][1]:
                path2end[sid]=(node,data['offsets'][sid])

        corrected=dict()
        for sid in data['offsets']:
            corrected[sid]=data['offsets'][sid]+offsets[sid]

        ng.node[node]['offsets']=corrected
    
    for node in ng:
        logging.debug("%s: %s"%(node,ng.node[node]['seq']))

    return ng,path2start,path2end

def refine_all(G,  **kwargs):
    realignbubbles=[]
    
    if kwargs['minsize']==None:
        kwargs['minsize']=kwargs['minlength']

    #detect all bubbles
    for b in bubbles.bubbles(G):

        if kwargs['complex']:
            if b.issimple():
                logging.debug("Skipping bubble %s, not complex."%str(b.nodes))
                continue

        if kwargs['simple']:
            if not b.issimple():
                logging.debug("Skipping bubble %s, not simple."%str(b.nodes))
                continue

        if b.minsize<kwargs['minsize']:
            logging.info("Skipping bubble %s, smallest allele (%dbp) is smaller than minsize=%d."%(str(b.nodes),b.minsize,kwargs['minsize']))
            continue

        if b.maxsize>kwargs['maxsize']:
            logging.warn("Skipping bubble %s, largest allele (%dbp) is larger than maxsize=%d."%(str(b.nodes),b.maxsize,kwargs['maxsize']))
            continue

        if kwargs['maxcumsize']!=None:
            if b.cumsize>kwargs['maxcumsize']:
                logging.warn("Skipping bubble %s, cumulative size %d is larger than maxcumsize=%d."%(str(b.nodes),b.cumsize,kwargs['maxcumsize']))
                continue

        if b.cumsize<kwargs['mincumsize']:
            logging.info("Skipping bubble %s, cumulative size %d is smaller than mincumsize=%d."%(str(b.nodes),b.cumsize,kwargs['mincumsize']))
            continue

        if len(b.nodes)==3:
            logging.info("Skipping bubble %s, indel, no point in realigning."%(str(b.nodes)))
            continue

        # sourcesamples=set(G.node[b.source]['offsets'].keys())
        # sinksamples=set(G.node[b.sink]['offsets'].keys())
        realignbubbles.append(b)

    distinctbubbles=[]
    for b1 in realignbubbles:
        for b2 in realignbubbles:
            if set(b2.nodes).issuperset(set(b1.nodes)) and not set(b1.nodes)==set(b2.nodes):
                logging.debug("Skipping bubble %s, because its nested in %s."%(str(b1.nodes),str(b2.nodes)))
                break
        else:
            distinctbubbles.append(b1)

    logging.info("Realigning a total of %d bubbles"%len(distinctbubbles))
    nn=max([node for node in G.nodes() if type(node)==int])+1

    if kwargs['nproc']>1:
        original_sigint_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        pool = Pool(processes=kwargs['nproc'])
        signal.signal(signal.SIGINT, original_sigint_handler)
        results=[]
        try:
            for bubble in distinctbubbles:
                G.node[bubble.source]['aligned']=1
                G.node[bubble.sink]['aligned']=1

                logging.debug("Submitting realign bubble between <%s> and <%s>, cumulative size %dbp (in nodes=%d)."%(bubble.source,bubble.sink,bubble.cumsize,len(bubble.nodes)-2))
                bnodes=list(set(bubble.nodes)-set([bubble.source,bubble.sink]))
                sg=G.subgraph(bnodes).copy()
                
                offsets=dict()
                for sid in G.node[bubble.source]['offsets']:
                    offsets[sid]=G.node[bubble.source]['offsets'][sid]+len(G.node[bubble.source]['seq'])

                sourcesamples=set(G.node[bubble.source]['offsets'].keys())
                sinksamples=set(G.node[bubble.sink]['offsets'].keys())
                paths=sourcesamples.intersection(sinksamples)

                results.append((bubble,pool.apply_async(refine_bubble,(sg,bubble,offsets,paths),kwargs)))

        except KeyboardInterrupt:
            pool.terminate()
        else:
            pool.close()
        pool.join()
        for bubble,r in results:
            logging.debug("Retrieving bubble realign results for bubble: %s - %s."%(bubble.source,bubble.sink))
            ng,path2start,path2end=r.get()
            G,nn=replace_bubble(G,bubble,ng,path2start,path2end,nn)
    else:
        for bubble in distinctbubbles:
            
            G.node[bubble.source]['aligned']=1
            G.node[bubble.sink]['aligned']=1

            bnodes=list(set(bubble.nodes)-set([bubble.source,bubble.sink]))
            sg=G.subgraph(bnodes)
            
            offsets=dict()
            for sid in G.node[bubble.source]['offsets']:
                offsets[sid]=G.node[bubble.source]['offsets'][sid]+len(G.node[bubble.source]['seq'])

            sourcesamples=set(G.node[bubble.source]['offsets'].keys())
            sinksamples=set(G.node[bubble.sink]['offsets'].keys())
            paths=sourcesamples.intersection(sinksamples)

            res=refine_bubble(sg,bubble,offsets,paths, **kwargs)
            if res==None:
                continue
            else:
                ng,path2start,path2end=res
                G,nn=replace_bubble(G,bubble,ng,path2start,path2end,nn)
    return G

def msa2graph(aobjs,idoffset=0,msa='muscle',parameters="",minconf=0):

    nn=idoffset
    ng=nx.DiGraph()
    ng.graph['paths']=[]
    ng.graph['path2id']=dict()
    ng.graph['id2path']=dict()
    ng.graph['id2end']=dict()

    for name,seq in aobjs:
        sid=len(ng.graph['paths'])
        ng.graph['path2id'][name]=sid
        ng.graph['id2path'][sid]=name
        ng.graph['id2end'][sid]=len(seq)
        ng.graph['paths'].append(name)

    #TODO: writing to a temporary file (for now), but this should ideally happen in memory
    uid=uuid.uuid4().hex
    tempfiles=[]
    logging.debug("Trying to construct MSA with %s."%msa)

    if msa in {'muscle','pecan','msaprobs','probcons'}:

        if msa=='muscle':
            cmd="muscle -in %s.fasta -quiet"%uid
            fasta_writer(uid+".fasta",aobjs)
            tempfiles.append("%s.fasta"%uid)
        elif msa=='probcons':
            # cmd="probcons %s.fasta -pre 1 -annot %s.conf"%(uid,uid)
            cmd="probcons %s.fasta -annot %s.conf %s"%(uid,uid,parameters) #-p /Users/jasperlinthorst/Documents/phd/probcons/nw.txt
            fasta_writer(uid+".fasta",aobjs)
            tempfiles.append("%s.fasta"%uid)
            tempfiles.append("%s.conf"%uid)
        elif msa=='pecan':
            cmd="java -cp /Users/jasperlinthorst/Documents/phd/pecan bp/pecan/Pecan -G %s.fasta -F %s.*.fasta -l -p %s.conf %s && cat %s.fasta"%(uid,uid,uid,parameters,uid)
            for i,(name,seq) in enumerate(aobjs): #pecan wants sequence in separate files
                fasta_writer("%s.%d.fasta"%(uid,i),[(name,seq)])
                tempfiles.append("%s.%d.fasta"%(uid,i))
            tempfiles.append("%s.fasta"%uid)
            tempfiles.append("%s.conf"%uid)
        elif msa=='msaprobs':
            cmd="msaprobs %s.fasta -annot %s.conf %s"%(uid,uid,parameters)
            fasta_writer(uid+".fasta",aobjs)
            tempfiles.append("%s.fasta"%uid)
            tempfiles.append("%s.conf"%uid)
        else:
            logging.fatal("Unkown multiple sequence aligner: %s"%msa)
            sys.exit(1)
        
        seqs=[""]*len(aobjs)
        names=[""]*len(aobjs)

        try:
            DEVNULL = open(os.devnull, 'wb')
            for a in subprocess.check_output([cmd],shell=True,stderr=DEVNULL).split(">")[1:]:
                x=a.find('\n')
                name=a[:x]
                seq=a[x+1:].replace("\n","")
                names[ng.graph['path2id'][name]]=name
                seqs[ng.graph['path2id'][name]]=seq
        except Exception as e:
            logging.fatal("System call to %s failed: \"%s\""%(msa,e.output))
            return

        confidence=[100]*len(seq) #initialize to 100% accuracy for each column

        if os.path.exists("%s.conf"%uid): #if there's an annotation file that accompanies the msa
            with open("%s.conf"%uid) as annot:
                for i,line in enumerate(annot):
                    confidence[i]=float(line.strip()) #expected percentage of correct pairwise matches in the i'th column of the msa...
                    if confidence[i]<1: #consider it a ratio, otherwise a percentage
                        confidence[i]=confidence[i]*100

    else:
        import probconslib
        logging.debug("Using probcons (in memory)")
        pl=probconslib.probcons()
        aln=pl.align(aobjs,consistency=0,refinement=0,pretraining=0)
        seqs=[""]*len(aobjs)
        names=[""]*len(aobjs)
        for name,seq in aln[0]:
            names[ng.graph['path2id'][name]]=name
            seqs[ng.graph['path2id'][name]]=seq
        confidence=aln[1]

        for i,seq in enumerate(seqs):
            logging.debug("OUT %s: %s"%(str(i).rjust(4, ' '),seq[0:400]))
        logging.debug("CONF    : %s"%"".join([str(c/10) for c in confidence[0:400]]))
    
    offsets={o:-1 for o in range(len(seqs))}
    nid=nn
    for i in xrange(len(seqs[0])):
        col={}
        base2node={}
        sid2node={}
        p=confidence[i]

        for j in xrange(len(seqs)):
            if seqs[j][i] in col:
                col[seqs[j][i]].add(j)
            else:
                col[seqs[j][i]]=set([j])
            if seqs[j][i]!='-':
                offsets[j]+=1

        for base in col:
            if i==0:
                assert(len(col[base])>0)
                # if len(col[base])>0:
                if p>=minconf:
                    ng.add_node(nid,seq=base,offsets={sid:offsets[sid] for sid in offsets if sid in col[base]},p=[p])
                    base2node[base]=nid
                    for sid in col[base]:
                        assert(sid not in sid2node)
                        sid2node[sid]=nid
                    nid+=1
                else: #new node per sequence
                    for sid in col[base]:
                        ng.add_node(nid,seq=base,offsets={sid:offsets[sid]},p=[p])
                        assert(sid not in sid2node)
                        sid2node[sid]=nid
                        if base in base2node:
                            base2node[base].append(nid)
                        else:
                            base2node[base]=[nid]
                        nid+=1
            else:

                if p>=minconf and pp>=minconf:
                    for pbase in pcol:
                        overlap=pcol[pbase].intersection(col[base])
                        if len(overlap)==0:
                            continue
                        elif len(overlap)==len(col[base])==len(pcol[pbase]): #append seq
                            ng.node[pbase2node[pbase]]['seq']+=base
                            ng.node[pbase2node[pbase]]['p']+=[p]
                            
                            base2node[base]=pbase2node[pbase]
                            
                            for sid in overlap:
                                assert(sid not in sid2node)
                                sid2node[sid]=sid2pnode[sid]
                        else:
                            assert(len(overlap)>0)
                            if base not in base2node: #if not already there
                                ng.add_node(nid,seq=base,offsets=dict(),p=[p])
                                base2node[base]=nid
                                for sid in col[base]:
                                    assert(sid not in sid2node)
                                    sid2node[sid]=nid
                                nid+=1
                            for sid in overlap:
                                ng.node[base2node[base]]['offsets'][sid]=offsets[sid]

                            ng.add_edge(pbase2node[pbase],base2node[base],paths=overlap,oto='+',ofrom='+')

                elif p<minconf and pp>=minconf:
                    
                    for sid in col[base]:
                        ng.add_node(nid,seq=base,offsets={sid:offsets[sid]},p=[p])
                        ng.add_edge(sid2pnode[sid],nid,paths={sid},oto='+',ofrom='+')
                        sid2node[sid]=nid

                        if base in base2node:
                            base2node[base].append(nid)
                        else:
                            base2node[base]=[nid]
                        nid+=1

                elif p>=minconf and pp<minconf:
                    ng.add_node(nid,seq=base,offsets=dict(),p=[p])
                    for sid in col[base]:
                        ng.node[nid]['offsets'][sid]=offsets[sid]
                        if not ng.has_edge(sid2pnode[sid],nid):
                            ng.add_edge(sid2pnode[sid],nid,paths={sid},oto='+',ofrom='+')
                        else:
                            ng[sid2pnode[sid]][nid]['paths'].add(sid)
                        sid2node[sid]=nid
                        base2node[base]=nid
                    nid+=1

                elif p<minconf and pp<minconf:
                    for sid in col[base]:
                        ng.node[sid2pnode[sid]]['seq']+=base
                        ng.node[sid2pnode[sid]]['p'].append(p)
                    sid2node=sid2pnode

                else:
                    logging.error("Impossible combination!")
                    sys.exit(1)

        assert(len(sid2node)==len(seqs))
        sid2pnode=sid2node
        pbase2node=base2node
        pcol=col
        pp=p

    # write_gml(ng,"",outputfile="before.gml")

    #remove gaps from graph
    remove=[]
    for node,data in ng.nodes(data=True):
        incroffset=False
        if data['seq'][0]=='-':
            incroffset=True

        data['seq']=data['seq'].replace("-","")
        if data['seq']=="":
            remove.append(node)
        elif incroffset:
            for sid in data['offsets']:
                data['offsets'][sid]+=1

        if len(data['offsets'])>1:
            data['aligned']=1
        else:
            data['aligned']=0

    for node in remove:
        ine=ng.in_edges(node,data=True)
        oute=ng.out_edges(node,data=True)
        for in1,in2,ind in ine:
            for out1,out2,outd in oute:
                overlap=ind['paths'].intersection(outd['paths'])
                if len(overlap)>=1:
                    if ng.has_edge(in1,out2):
                        ng[in1][out2]['paths']=ng[in1][out2]['paths'] | overlap
                    else:
                        ng.add_edge(in1,out2,paths=overlap,ofrom='+',oto='+')

    ng.remove_nodes_from(remove)

    # write_gml(ng,"",outputfile="after.gml")

    for node in ng:
        logging.debug("%s: %s"%(node,ng.node[node]['seq']))


    for tmpfile in tempfiles:
        try:
            os.remove(tmpfile)
        except Exception as e:
            logging.fatal("Failed to remove tmp file: \"%s\""%tmpfile)
            return

    return ng