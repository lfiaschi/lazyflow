#Python
import time
import weakref
import itertools
from threading import Lock
import logging
logger = logging.getLogger(__name__)
from functools import partial

#SciPy
import numpy

#lazyflow
from lazyflow.request import RequestPool
from lazyflow.drtile import drtile
from lazyflow.roi import sliceToRoi, roiToSlice, getBlockBounds, TinyVector
from lazyflow.graph import InputSlot, OutputSlot
from lazyflow.utility import fastWhere, Tracer
from lazyflow.operators.opCache import OpCache
from lazyflow.operators.opArrayPiper import OpArrayPiper
from lazyflow.operators.arrayCacheMemoryMgr import ArrayCacheMemoryMgr, MemInfoNode

class OpArrayCache(OpCache):
    """ Allocates a block of memory as large as Input.meta.shape (==Output.meta.shape)
        with the same dtype in order to be able to cache results.
        
        blockShape: dirty regions are tracked with a granularity of blockShape
    """
    
    name = "ArrayCache"
    description = "numpy.ndarray caching class"
    category = "misc"

    DefaultBlockSize = 64

    #Input
    Input = InputSlot()
    blockShape = InputSlot(value = DefaultBlockSize)
    fixAtCurrent = InputSlot(value = False)
   
    #Output
    CleanBlocks = OutputSlot()
    Output = OutputSlot()

    loggingName = __name__ + ".OpArrayCache"
    logger = logging.getLogger(loggingName)
    traceLogger = logging.getLogger("TRACE." + loggingName)

    # Block states
    IN_PROCESS  = 0
    DIRTY       = 1
    CLEAN       = 2
    FIXED_DIRTY = 3

    def __init__(self, *args, **kwargs):
        super( OpArrayCache, self ).__init__(*args, **kwargs)
        self._origBlockShape = self.DefaultBlockSize
        self._blockShape = None
        self._dirtyShape = None
        self._blockState = None
        self._dirtyState = None
        self._fixed = False
        self._cache = None
        self._lock = Lock()
        self._cacheLock = Lock()
        self._lazyAlloc = True
        self._cacheHits = 0
        self._has_fixed_dirty_blocks = False
        self._memory_manager = ArrayCacheMemoryMgr.instance
        self._running = 0

    def usedMemory(self):
        if self._cache is not None:
            return self._cache.nbytes
        else:
            return 0

    def _blockShapeForIndex(self, index):
        cacheShape = numpy.array(self._cache.shape)
        blockStart = index * self._blockShape
        blockStop = numpy.minimum(blockStart + self._blockShape, cacheShape)
        
    def fractionOfUsedMemoryDirty(self):
        totAll   = numpy.prod(self.Output.meta.shape)
        totDirty = 0
        for i, v in enumerate(self._blockState.ravel()):
            sh = self._blockShapeForIndex(i)
            if sh is None:
                continue
            if v == self.DIRTY or v == self.FIXED_DIRTY:
                totDirty += numpy.prod(sh)
        return totDirty/float(totAll)
    
    def lastAccessTime(self):
        return self._last_access
        
    def generateReport(self, report):
        report.name = self.name
        report.fractionOfUsedMemoryDirty = self.fractionOfUsedMemoryDirty()
        report.usedMemory = self.usedMemory()
        report.lastAccessTime = self.lastAccessTime()
        report.dtype = self.Output.meta.dtype
        report.type = type(self)
        report.id = id(self)

    def _freeMemory(self, refcheck = True):
        with self._cacheLock:
            freed  = self.usedMemory()
            if self._cache is not None:
                fshape = self._cache.shape
                try:
                    self._cache.resize((1,), refcheck = refcheck)
                except ValueError:
                    freed = 0
                    self.logger.warn("OpArrayCache: freeing failed due to view references")
                if freed > 0:
                    self.logger.debug("OpArrayCache: freed cache of shape:{}".format(fshape))
    
                    self._lock.acquire()
                    self._blockState[:] = OpArrayCache.DIRTY
                    del self._cache
                    self._cache = None
                    self._lock.release()
            return freed

    def _allocateManagementStructures(self):
        with Tracer(self.traceLogger):
            shape = self.Output.meta.shape
            if type(self._origBlockShape) != tuple:
                self._blockShape = (self._origBlockShape,)*len(shape)
            else:
                self._blockShape = self._origBlockShape
    
            self._blockShape = numpy.minimum(self._blockShape, shape)
    
            self._dirtyShape = numpy.ceil(1.0 * numpy.array(shape) / numpy.array(self._blockShape))
    
            self.logger.debug("Configured OpArrayCache with shape={}, blockShape={}, dirtyShape={}, origBlockShape={}".format(shape, self._blockShape, self._dirtyShape, self._origBlockShape))
   
            #if a request has been submitted to get a block, the request object
            #is stored within this array
            self._blockQuery = numpy.ndarray(self._dirtyShape, dtype=object)
           
            #keep track of the dirty state of each block
            self._blockState = OpArrayCache.DIRTY * numpy.ones(self._dirtyShape, numpy.uint8)
    
            self._blockState[:]= OpArrayCache.DIRTY
            self._dirtyState = OpArrayCache.CLEAN
    
    def _allocateCache(self):
        with self._cacheLock:
            self._last_access = None
            self._cache_priority = 0
            self._running = 0

            if self._cache is None or (self._cache.shape != self.Output.meta.shape):
                mem = numpy.zeros(self.Output.meta.shape, dtype = self.Output.meta.dtype)
                self.logger.debug("OpArrayCache: Allocating cache (size: %dbytes)" % mem.nbytes)
                if self._blockState is None:
                    self._allocateManagementStructures()
                self._cache = mem
        self._memory_manager.add(self)

    def setupOutputs(self):
        self.CleanBlocks.meta.shape = (1,)
        self.CleanBlocks.meta.dtype = object
        

        reconfigure = False
        if  self.inputs["fixAtCurrent"].ready():
            self._fixed =  self.inputs["fixAtCurrent"].value

        if self.inputs["blockShape"].ready() and self.inputs["Input"].ready():
            newBShape = self.inputs["blockShape"].value
            if self._origBlockShape != newBShape and self.inputs["Input"].ready():
                reconfigure = True
            self._origBlockShape = newBShape
            self._blockShape = newBShape
            
            inputSlot = self.inputs["Input"]
            self.outputs["Output"].meta.assignFrom(inputSlot.meta)

        shape = self.Output.meta.shape
        if reconfigure and shape is not None:
            self._lock.acquire()
            self._allocateManagementStructures()
            if not self._lazyAlloc:
                self._allocateCache()
            self._lock.release()

    def propagateDirty(self, slot, subindex, roi):
        shape = self.Output.meta.shape
        
        key = roi.toSlice()
        if slot == self.inputs["Input"]:
            start, stop = sliceToRoi(key, shape)

            with self._lock:
                if self._blockState is not None:
                    blockStart = numpy.floor(1.0 * start / self._blockShape)
                    blockStop = numpy.ceil(1.0 * stop / self._blockShape)
                    blockKey = roiToSlice(blockStart,blockStop)
                    if self._fixed:
                        # Remember that this block became dirty while we were fixed 
                        #  so we can notify downstream operators when we become unfixed.
                        self._blockState[blockKey] = OpArrayCache.FIXED_DIRTY
                        self._has_fixed_dirty_blocks = True
                    else:
                        self._blockState[blockKey] = OpArrayCache.DIRTY

            if not self._fixed:
                self.outputs["Output"].setDirty(key)
        if slot == self.inputs["fixAtCurrent"]:
            if self.inputs["fixAtCurrent"].ready():
                self._fixed = self.inputs["fixAtCurrent"].value
                if not self._fixed and self.Output.meta.shape is not None and self._has_fixed_dirty_blocks:
                    # We've become unfixed, so we need to notify downstream 
                    #  operators of every block that became dirty while we were fixed.
                    # Convert all FIXED_DIRTY states into DIRTY states
                    with self._lock:
                        cond = (self._blockState[...] == OpArrayCache.FIXED_DIRTY)
                        self._blockState[...]  = fastWhere(cond, OpArrayCache.DIRTY, self._blockState, numpy.uint8)
                        self._has_fixed_dirty_blocks = False
                    newDirtyBlocks = numpy.transpose(numpy.nonzero(cond))
                    
                    # To avoid lots of setDirty notifications, we simply merge all the dirtyblocks into one single superblock.
                    # This should be the best option in most cases, but could be bad in some cases.
                    # TODO: Optimize this by merging the dirty blocks via connected components or something.
                    cacheShape = numpy.array(self.Output.meta.shape)
                    dirtyStart = cacheShape
                    dirtyStop = [0] * len(cacheShape)
                    for index in newDirtyBlocks:
                        blockStart = index * self._blockShape
                        blockStop = numpy.minimum(blockStart + self._blockShape, cacheShape)
                        
                        dirtyStart = numpy.minimum(dirtyStart, blockStart)
                        dirtyStop = numpy.maximum(dirtyStop, blockStop)

                    if len(newDirtyBlocks > 0):
                        self.Output.setDirty( dirtyStart, dirtyStop )

    def _updatePriority(self, new_access = None):
        if self._last_access is None:
            self._last_access = new_access or time.time()
        cur_time = time.time()
        delta = cur_time - self._last_access + 1e-9

        self._last_access = cur_time
        new_prio = 0.5 * self._cache_priority + delta
        self._cache_priority = new_prio

    def execute(self, slot, subindex, roi, result):
        if slot == self.Output:
            return self._executeOutput(slot, subindex, roi, result)
        elif slot == self.CleanBlocks:
            return self._executeCleanBlocks(slot, subindex, roi, result)
        
    def _executeOutput(self, slot, subindex, roi, result):
        key = roi.toSlice()

        shape = self.Output.meta.shape
        start, stop = sliceToRoi(key, shape)

        self.traceLogger.debug("Acquiring ArrayCache lock...")
        self._lock.acquire()
        self.traceLogger.debug("ArrayCache lock acquired.")

        ch = self._cacheHits
        ch += 1
        self._cacheHits = ch

        self._running += 1

        if self._cache is None:
            self._allocateCache()

        cacheView = self._cache[:] #prevent freeing of cache during running this function


        blockStart = (1.0 * start / self._blockShape).floor()
        blockStop = (1.0 * stop / self._blockShape).ceil()
        blockKey = roiToSlice(blockStart,blockStop)

        blockSet = self._blockState[blockKey]

        # this is a little optimization to shortcut
        # many lines of python code when all data is
        # is already in the cache:
        if numpy.logical_or(blockSet == OpArrayCache.CLEAN, blockSet == OpArrayCache.FIXED_DIRTY).all():
            result[:] = self._cache[roiToSlice(start, stop)]
            self._running -= 1
            self._updatePriority()
            cacheView = None
            self._lock.release()
            return

        inProcessQueries = numpy.unique(numpy.extract( blockSet == OpArrayCache.IN_PROCESS, self._blockQuery[blockKey]))

        cond = (blockSet == OpArrayCache.DIRTY)
        tileWeights = fastWhere(cond, 1, 128**3, numpy.uint32)
        trueDirtyIndices = numpy.nonzero(cond)

        tileArray = drtile.test_DRTILE(tileWeights, 128**3).swapaxes(0,1)

        dirtyRois = []
        half = tileArray.shape[0]/2
        dirtyPool = RequestPool()

        def onCancel(req):
            return False # indicate that this request cannot be canceled

        self.traceLogger.debug("Creating cache input requests")
        for i in range(tileArray.shape[1]):

            drStart3 = tileArray[:half,i]
            drStop3 = tileArray[half:,i]
            drStart2 = drStart3 + blockStart
            drStop2 = drStop3 + blockStart
            drStart = drStart2*self._blockShape
            drStop = drStop2*self._blockShape

            shape = self.Output.meta.shape
            drStop = numpy.minimum(drStop, shape)
            drStart = numpy.minimum(drStart, shape)

            key3 = roiToSlice(drStart3,drStop3)
            key2 = roiToSlice(drStart2,drStop2)

            key = roiToSlice(drStart,drStop)

            if not self._fixed:
                dirtyRois.append([drStart,drStop])

                req = self.inputs["Input"][key].writeInto(self._cache[key])

                req.uncancellable = True #FIXME
                
                dirtyPool.add(req)

                self._blockQuery[key2] = weakref.ref(req)

                #sanity check:
                if (self._blockState[key2] != OpArrayCache.DIRTY).any():
                    print "original condition", cond
                    print "original tilearray", tileArray, tileArray.shape
                    print "original tileWeights", tileWeights, tileWeights.shape
                    print "sub condition", self._blockState[key2] == OpArrayCache.DIRTY
                    print "START, STOP", drStart2, drStop2
                    import h5py
                    with h5py.File("test.h5", "w") as f:
                        f.create_dataset("data",data = tileWeights)
                        print "%r \n %r \n %r\n %r\n %r \n%r" % (key2, blockKey,self._blockState[key2], self._blockState[blockKey][trueDirtyIndices],self._blockState[blockKey],tileWeights)
                    assert False
                self._blockState[key2] = OpArrayCache.IN_PROCESS

        # indicate the inprocessing state, by setting array to 0 (i.e. IN_PROCESS)
        if not self._fixed:
            blockSet[:]  = fastWhere(cond, OpArrayCache.IN_PROCESS, blockSet, numpy.uint8)
        else:
            # Someone asked for some dirty blocks while we were fixed.
            # Mark these blocks to be signaled as dirty when we become unfixed
            blockSet[:]  = fastWhere(cond, OpArrayCache.FIXED_DIRTY, blockSet, numpy.uint8)
            self._has_fixed_dirty_blocks = True
        self._lock.release()

        temp = itertools.count(0)

        #wait for all requests to finish
        self.traceLogger.debug( "Firing all {} cache input requests...".format(len(dirtyPool)) )
        dirtyPool.wait()
        if len( dirtyPool ) > 0:
            # Signal that something was updated.
            # Note that we don't need to do this for the 'in process' queries (below)  
            #  because they are already in the dirtyPool in some other thread
            self.Output._sig_value_changed()
        dirtyPool.clean()
        self.traceLogger.debug( "All cache input requests received." )

        # indicate the finished inprocess state (i.e. CLEAN)
        if not self._fixed and temp.next() == 0:
            with self._lock:
                blockSet[:] = fastWhere(cond, OpArrayCache.CLEAN, blockSet, numpy.uint8)
                self._blockQuery[blockKey] = fastWhere(cond, None, self._blockQuery[blockKey], object)

        inProcessPool = RequestPool()
        #wait for all in process queries
        for req in inProcessQueries:
            req = req() # get original req object from weakref
            if req is not None:
                inProcessPool.add(req) 

        inProcessPool.wait()
        inProcessPool.clean()

        # finally, store results in result area
        self._lock.acquire()
        if self._cache is not None:
            result[:] = self._cache[roiToSlice(start, stop)]
        else:
            self.traceLogger.debug( "WAITING FOR INPUT WITH THE CACHE LOCK LOCKED!" )
            self.inputs["Input"][roiToSlice(start, stop)].writeInto(result).wait()
            self.traceLogger.debug( "INPUT RECEIVED WITH THE CACHE LOCK LOCKED." )
        self._running -= 1
        self._updatePriority()
        cacheView = None

        self._lock.release()

    def setInSlot(self, slot, subindex, roi, value):
        assert slot == self.inputs["Input"]
        ch = self._cacheHits
        ch += 1
        self._cacheHits = ch
        start, stop = roi.start, roi.stop
        blockStart = numpy.ceil(1.0 * start / self._blockShape)
        blockStop = numpy.floor(1.0 * stop / self._blockShape)
        blockStop = numpy.where(stop == self.Output.meta.shape, self._dirtyShape, blockStop)
        blockKey = roiToSlice(blockStart,blockStop)

        if (self._blockState[blockKey] != OpArrayCache.CLEAN).any():
            start2 = blockStart * self._blockShape
            stop2 = blockStop * self._blockShape
            stop2 = numpy.minimum(stop2, self.Output.meta.shape)
            key2 = roiToSlice(start2,stop2)
            self._lock.acquire()
            if self._cache is None:
                self._allocateCache()
            self._cache[key2] = value[roiToSlice(start2-start,stop2-start)]
            self._blockState[blockKey] = self._dirtyState
            self._blockQuery[blockKey] = None
            self._lock.release()

    def _executeCleanBlocks(self, slot, subindex, roi, destination):
        indexCols = numpy.where(self._blockState == OpArrayCache.CLEAN)
        clean_block_starts = numpy.array(indexCols).transpose()
            
        inputShape = self.Input.meta.shape
        clean_block_rois = map( partial( getBlockBounds, inputShape, self._blockShape ),
                                clean_block_starts )
        destination[0] = map( partial(map, TinyVector), clean_block_rois )
        return destination
