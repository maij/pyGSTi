import hashlib   as _hashlib
import functools as _functools
import sys       as _sys
import numpy     as _np
import functools as _functools
import inspect   as _inspect

from collections import Counter, defaultdict
from pprint      import pprint

from . import compattools as _compat

from .opttools import timed_block as _timed_block

DIGEST_TIMES = defaultdict(list)

def average(l):
    nCalls = max(1, len(l))
    return sum(l) / nCalls

def show_cache_percents(hits, misses, printer):
    size = lambda counter : len(list(counter.elements()))
    nHits     = size(hits)
    nMisses   = size(misses)
    nRequests = nHits + nMisses 
    printer.log('    {:<10} requests'.format(nRequests))
    printer.log('    {:<10} hits'.format(nHits))
    printer.log('    {:<10} misses'.format(nMisses))
    printer.log('    {}% effective\n'.format(round((nHits/max(1, nRequests)) * 100, 2)))

def show_kvs(title, kvs, printer):
    printer.log(title)
    for k, v in kvs:
        printer.log('    {:<40} {}'.format(k, v))
    printer.log('')

class SmartCache(object):
    StaticCacheList = []

    def __init__(self, decorating=(None, None)):
        self.cache       = dict()
        self.ineffective = set()
        self.decoratingModule, self.decoratingFn = decorating
        self.customDigests = []

        self.misses = Counter()
        self.hits   = Counter()
        self.fhits  = Counter()

        self.requests            = Counter()
        self.ineffectiveRequests = Counter()

        self.effectiveTimes   = defaultdict(list)
        self.ineffectiveTimes = defaultdict(list)

        self.hashTimes = defaultdict(list)
        self.callTimes = defaultdict(list)

        self.typesigs = dict()
        self.saved = 0
        
        SmartCache.StaticCacheList.append(self)

    def add_digest(self, custom):
        self.customDigests.append(custom)

    def low_overhead_cached_compute(self, fn, argVals, kwargs=None):
        if kwargs is None:
            kwargs = dict()
        name_key = get_fn_name_key(fn)
        if name_key in self.ineffective:
            key = 'NA'
            result = fn(*argVals, **kwargs)
        else:
            times = dict()
            with _timed_block('hash', times):
                key = call_key(fn, (argVals, kwargs), self.customDigests) # cache by call key
            if key not in self.cache:
                with _timed_block('call', times):
                    self.cache[key] = fn(*argVals, **kwargs)
                if times['hash'] > times['call']:
                    self.ineffective.add(name_key)
            result = self.cache[key]
        return key, result

    def cached_compute(self, fn, argVals, kwargs=None):
        if kwargs is None:
            kwargs = dict()
        name_key = get_fn_name_key(fn)
        self.requests[name_key] += 1 
        if name_key in self.ineffective:
            key = 'NA'
            result = fn(*argVals, **kwargs)
            self.ineffectiveRequests[name_key] += 1
            self.misses[key] += 1
        else:
            times = dict()
            with _timed_block('hash', times):
                key = call_key(fn, (argVals, kwargs), self.customDigests) # cache by call key
            if key not in self.cache:
                typesig = str(tuple(str(type(arg)) for arg in argVals)) + \
                        str({k : str(type(v)) for k, v in kwargs.items()})
                self.typesigs[name_key] = typesig
                with _timed_block('call', times):
                    self.cache[key] = fn(*argVals, **kwargs)
                self.misses[key] += 1
                hashtime = times['hash']
                calltime = times['call']
                if hashtime > calltime:
                    self.ineffective.add(name_key)
                    self.ineffectiveTimes[name_key].append(hashtime - calltime)
                else:
                    self.effectiveTimes[name_key].append(calltime - hashtime)
                self.hashTimes[name_key].append(hashtime)
                self.callTimes[name_key].append(calltime)
            else:
                #print('The function {} experienced a cache hit'.format(name_key))
                self.hits[key] += 1
                self.fhits[name_key] += 1
            result = self.cache[key]
        return key, result

    @staticmethod
    def global_status(printer):
        totalSaved  = 0
        totalHits   = Counter()
        totalMisses = Counter()

        suggestRemove = set()

        for cache in SmartCache.StaticCacheList:
            cache.status(printer)
            totalHits   += cache.hits
            totalMisses += cache.misses
            totalSaved  += cache.saved

            if cache.decoratingFn is not None and \
                    cache.decoratingFn in cache.ineffective:
                suggestRemove.add(cache.decoratingModule + '.' + cache.decoratingFn)

        with printer.verbosity_env(2):
            printer.log('Average hash times by object:')
            for k, v in sorted(DIGEST_TIMES.items(), key=lambda t : sum(t[1])):
                total = sum(v)
                avg   = average(v)
                printer.log('    {:<65} avg | {}s'.format(k, avg))
                printer.log('    {:<65} tot | {}s'.format('', total))
                printer.log('-'*100)

        printer.log('')
        printer.log('Because they take longer to hash than to calculate, \n' + \
                    'the following functions may be unmarked for caching:')
        for name in suggestRemove:
            printer.log('    {}'.format(name))

        printer.log('Global cache overview:')
        show_cache_percents(totalHits, totalMisses, printer)
        printer.log('    {} seconds saved total'.format(totalSaved))

    def avg_timedict(self, d):
        ret = dict()
        for k, v in d.items():
            if k not in self.fhits:
                time = 0
            else:
                time = average(v) * len(v)
            ret[k] = time
        return ret

    def status(self, printer):
        printer.log('Status of smart cache decorating {}.{}:\n'.format(
            self.decoratingModule, self.decoratingFn))
        show_cache_percents(self.hits, self.misses, printer)

        with printer.verbosity_env(2):
            show_kvs('Most common requests:\n', self.requests.most_common(), printer)
            show_kvs('Ineffective requests:\n', self.ineffectiveRequests.most_common(), printer)
            show_kvs('Hits:\n', self.fhits.most_common(), printer)

            printer.log('Type signatures of functions and their hash times:\n')
            for k, v in self.typesigs.items():
                avg = average(self.hashTimes[k])
                printer.log('    {:<40} {}'.format(k, v))
                printer.log('    {:<40} {}'.format(k, avg))
                printer.log('')
            printer.log('')

            savedTimes = self.avg_timedict(self.effectiveTimes)
            saved = sum(savedTimes.values())
            show_kvs('Effective total saved time:\n', 
                    sorted(savedTimes.items(), key=lambda t : t[1], reverse=True), 
                    printer)

            overTimes = self.avg_timedict(self.ineffectiveTimes)
            overhead = sum(overTimes.values())
            show_kvs('Ineffective differences:\n', 
                    sorted(overTimes.items(), key=lambda t : t[1]),
                    printer)

        printer.log('overhead    : {}'.format(overhead))
        printer.log('saved       : {}'.format(saved))
        printer.log('net benefit : {}'.format(saved - overhead))
        self.saved = saved - overhead

def smart_cached(obj):
    cache = obj.cache = SmartCache(decorating=(obj.__module__, obj.__name__))
    @_functools.wraps(obj)
    def cacher(*args, **kwargs):
        k, v = cache.cached_compute(obj, args, kwargs)
        return v
    return cacher

class CustomDigestError(Exception):
    pass

def digest(obj, custom_digests=None):
    """Returns an MD5 digest of an arbitary Python object, `obj`."""
    if custom_digests is None:
        custom_digests = []
    if _sys.version_info > (3, 0): # Python3?
        longT = int      # define long and unicode
        unicodeT = str   #  types to mimic Python2
    else:
        longT = long
        unicodeT = unicode

    # a function to recursively serialize 'v' into an md5 object
    def add(md5, v):
        """Add `v` to the hash, recursively if needed."""
        with _timed_block(str(type(v)), DIGEST_TIMES):
            md5.update(str(type(v)).encode('utf-8'))
            if isinstance(v, bytes):
                md5.update(v)  #can add bytes directly
            elif isinstance(v, float) or _compat.isstr(v) or _compat.isint(v):
                md5.update(str(v).encode('utf-8')) #need to encode strings
            elif isinstance(v, _np.ndarray):
                md5.update(v.tostring()) # numpy gives us bytes
            elif isinstance(v, (tuple, list)):
                for el in v:  add(md5,el)
            elif isinstance(v, dict):
                keys = list(v.keys())
                for k in sorted(keys):
                    add(md5,k)
                    add(md5,v[k])
            elif v is None:
                md5.update("NONE".encode('utf-8'))
            elif hasattr(v, 'timestamp'):
                md5.update(v.timestamp.encode('utf-8'))
            else:
                for custom_digest in custom_digests:
                    try:
                        custom_digest(md5, v)
                        break
                    except CustomDigestError:
                        pass
                else:
                    attribs = list(sorted(dir(v)))
                    for k in attribs:
                        if k.startswith('__'): continue
                        a = getattr(v, k)
                        if _inspect.isroutine(a): continue
                        add(md5,k)
                        add(md5,a)
            return

    M = _hashlib.md5()
    add(M, obj)
    return M.digest() #return the MD5 digest

def get_fn_name_key(fn):
    name = fn.__name__
    if hasattr(fn, '__self__'):
        name = fn.__self__.__class__.__name__ + '.' + name
    return name

def call_key(fn, args, custom_digests):
    """ 
    Returns a hashable key for caching the result of a function call.

    Parameters
    ----------
    fn : function
       The function itself

    args : list or tuple
       The function's arguments.

    Returns
    -------
    tuple
    """
    fnName = get_fn_name_key(fn)
    inner_digest = _functools.partial(digest, custom_digests=custom_digests)
    return (fnName,) + tuple(map(inner_digest,args))
