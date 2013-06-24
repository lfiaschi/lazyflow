#Python
import copy
import logging
logger = logging.getLogger(__name__)
traceLogger = logging.getLogger("TRACE." + __name__)

#SciPy
import numpy
import vigra

#lazyflow
from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow import roi
from lazyflow.roi import roiToSlice, sliceToRoi

def axisTagObjectFromFlag(flag):

    if flag in ['x','y','z']:
        type=vigra.AxisType.Space
    elif flag=='c':
        type=vigra.AxisType.Channel
    elif flag=='t':
        type=vigra.AxisType.Time
    else:
        print "Requested flag", str(flag)
        raise

    return vigra.AxisTags(vigra.AxisInfo(flag,type))


def axisType(flag):
    if flag in ['x','y','z']:
        return vigra.AxisType.Space
    elif flag=='c':
        return vigra.AxisType.Channels

    elif flag=='t':
        return vigra.AxisType.Time
    else:
        raise


def axisTagsToString(axistags):
    res=[]
    for axistag in axistags:
        res.append(axistag.key)
    return res


def getSubKeyWithFlags(key,axistags,axisflags):
    assert len(axistags)==len(key)
    assert len(axisflags)<=len(key)

    d=dict(zip(axisTagsToString(axistags),key))

    newKey=[]
    for flag in axisflags:
        slice=d[flag]
        newKey.append(slice)

    return tuple(newKey)


def popFlagsFromTheKey(key,axistags,flags):
    d=dict(zip(axisTagsToString(axistags),key))

    newKey=[]
    for flag in axisTagsToString(axistags):
        if flag not in flags:
            slice=d[flag]
            newKey.append(slice)

    return newKey


class OpMultiArraySlicer(Operator):
    """
    Produces a list of image slices along the given axis.
    Same as the slicer operator below, but reduces the dimensionality of the data.
    The sliced axis is discarded in the output image shape.
    """
    inputSlots = [InputSlot("Input"),InputSlot('AxisFlag')]
    outputSlots = [OutputSlot("Slices",level=1)]

    name = "Multi Array Slicer"
    category = "Misc"

    def setupOutputs(self):
        flag=self.inputs["AxisFlag"].value

        indexAxis=self.inputs["Input"].meta.axistags.index(flag)
        outshape=list(self.inputs["Input"].meta.shape)
        n=outshape.pop(indexAxis)
        outshape=tuple(outshape)

        outaxistags=copy.copy(self.inputs["Input"].meta.axistags)
        del outaxistags[flag]

        self.outputs["Slices"].resize(n)

        for o in self.outputs["Slices"]:
            # Output metadata is a modified copy of the input's metadata
            o.meta.assignFrom( self.Input.meta )
            o.meta.axistags = outaxistags
            o.meta.shape = outshape
            if self.Input.meta.drange is not None:
                o.meta.drange = self.Input.meta.drange

    def execute(self, slot, subindex, rroi, result):
        key = roiToSlice(rroi.start, rroi.stop)
        index = subindex[0]
        #print "SLICER: key", key, "indexes[0]", indexes[0], "result", result.shape
        start,stop=roi.sliceToRoi(key,self.outputs["Slices"][index].meta.shape)

        start=list(start)
        stop=list(stop)

        flag=self.inputs["AxisFlag"].value
        indexAxis=self.inputs["Input"].meta.axistags.index(flag)

        start.insert(indexAxis,index)
        stop.insert(indexAxis,index)

        newKey=roi.roiToSlice(numpy.array(start),numpy.array(stop))

        ttt = self.inputs["Input"][newKey].wait()

        writeKey = [slice(None, None, None) for k in key]
        writeKey.insert(indexAxis, 0)
        writeKey = tuple(writeKey)

        return ttt[writeKey ]#+ (0,)]

    def propagateDirty(self, slot, subindex, roi):
        if slot == self.AxisFlag:
            for i,s in enumerate(self.Slices):
                s.setDirty( slice(None) )
        elif slot == self.Input:
            key = roi.toSlice()
            reducedKey = list(key)
            inputTags = self.Input.meta.axistags
            flag = self.AxisFlag.value
            axisSlice = reducedKey.pop( inputTags.index(flag) )
            
            axisStart, axisStop = axisSlice.start, axisSlice.stop
            if axisStart is None:
                axisStart = 0
            if axisStop is None:
                axisStop = len( self.Slices )
    
            for i in range(axisStart, axisStop):
                self.Slices[i].setDirty( reducedKey )
        else:
            assert False, "Unknown dirty input slot"
        

class OpMultiArraySlicer2(Operator):
    """
    Produces a list of image slices along the given axis.
    Same as the slicer operator above, but does not reduce the dimensionality of the data.
    The output image shape will have a dimension of 1 for the axis that was sliced.
    """
    #FIXME: This operator return a sigleton in the sliced direction
    #Should be integrated with the above one to have a more consistent notation
    inputSlots = [InputSlot("Input"),InputSlot('AxisFlag'), InputSlot("SliceIndexes", optional=True)]
    outputSlots = [OutputSlot("Slices",level=1)]

    name = "Multi Array Slicer"
    category = "Misc"

    def __init__(self, *args, **kwargs):
        super(OpMultiArraySlicer2, self).__init__(*args, **kwargs)
        self.inputShape = None

    def setupOutputs(self):
        flag=self.inputs["AxisFlag"].value

        indexAxis=self.inputs["Input"].meta.axistags.index(flag)
        inshape=list(self.inputs["Input"].meta.shape)
        outshape = list(inshape)
        outshape.pop(indexAxis)
        outshape.insert(indexAxis, 1)
        outshape=tuple(outshape)

        outaxistags=copy.copy(self.inputs["Input"].meta.axistags)

        sliceIndexes = self.getSliceIndexes()
        self.outputs["Slices"].resize( len(sliceIndexes) )

        for oslot in self.Slices:
            # Output metadata is a modified copy of the input's metadata
            oslot.meta.assignFrom( self.Input.meta )
            oslot.meta.axistags = outaxistags
            oslot.meta.shape = outshape
            if self.Input.meta.drange is not None:
                oslot.meta.drange = self.Input.meta.drange

        inputShape = self.Input.meta.shape
        if self.inputShape != inputShape:
            self.inputShape = inputShape
            for oslot in self.Slices:
                oslot.setDirty(slice(None))

    def getSliceIndexes(self):
        if self.SliceIndexes.ready():
            return self.SliceIndexes.value
        else:
            # Default is all indexes of the sliced axis
            flag = self.inputs["AxisFlag"].value
            axistags = self.inputs["Input"].meta.axistags
            indexAxis = axistags.index(flag)
            inshape = self.inputs["Input"].meta.shape
            return list( range( inshape[indexAxis] ) )
    
    def execute(self, slot, subindex, rroi, result):
        key = roiToSlice(rroi.start, rroi.stop)
        index = subindex[0]
        # Index of the input slice this data will come from.
        sliceIndex = self.getSliceIndexes()[index]

        outshape = self.outputs["Slices"][index].meta.shape
        start,stop=roi.sliceToRoi(key,outshape)

        start=list(start)
        stop=list(stop)

        flag=self.inputs["AxisFlag"].value
        indexAxis=self.inputs["Input"].meta.axistags.index(flag)

        start.pop(indexAxis)
        stop.pop(indexAxis)

        start.insert(indexAxis, sliceIndex)
        stop.insert(indexAxis, sliceIndex)

        newKey=roi.roiToSlice(numpy.array(start),numpy.array(stop))

        self.inputs["Input"][newKey].writeInto(result).wait()
        return result

    def propagateDirty(self, inputSlot, subindex, roi):
        if inputSlot == self.AxisFlag or inputSlot == self.SliceIndexes:
            # AxisFlag or slice set changed.  Everything is dirty
            for i, slot in enumerate(self.Slices):
                slot.setDirty(slice(None))
        elif inputSlot == self.Input:
            # Mark each of the intersected slices as dirty
            channelAxis = self.Input.meta.axistags.index('c')
            channels = zip(roi.start, roi.stop)[channelAxis]
            for i in range(*channels):
                if i < len(self.Slices):
                    slot = self.Slices[i]
                    sliceRoi = copy.copy(roi)
                    sliceRoi.start[channelAxis] = 0
                    sliceRoi.stop[channelAxis] = 1
                    slot.setDirty(sliceRoi)
        else:
            assert False, "Unknown dirty input slot."


class OpMultiArrayStacker(Operator):
    inputSlots = [InputSlot("Images", level=1), InputSlot("AxisFlag"), InputSlot("AxisIndex", optional=True)]
    outputSlots = [OutputSlot("Output")]

    name = "Multi Array Stacker"
    description = "Stack inputs on any axis, including the ones which are not there yet"
    category = "Misc"

    def setupOutputs(self):
        #This function is needed so that we don't depend on the order of connections.
        #If axis flag or axis index is connected after the input images, the shape is calculated
        #here
        self.setRightShape()

    def setRightShape(self):
        c = 0
        flag = self.inputs["AxisFlag"].value
        self.intervals = []

        inTagKeys = []

        for inSlot in self.inputs["Images"]:
            inTagKeys = [ax.key for ax in inSlot.meta.axistags]
            if inSlot.partner is not None:
                self.outputs["Output"].meta.dtype = inSlot.meta.dtype
                self.outputs["Output"].meta.axistags = copy.copy(inSlot.meta.axistags)
                #indexAxis=inSlot.meta.axistags.index(flag)

                outTagKeys = [ax.key for ax in self.outputs["Output"].meta.axistags]

                if not flag in outTagKeys:
                    if self.AxisIndex.ready():
                        axisindex = self.AxisIndex.value
                    else:
                        axisindex = len( outTagKeys )
                    self.outputs["Output"].meta.axistags.insert(axisindex, vigra.AxisInfo(flag, axisType(flag)))

                old_c = c
                if flag in inTagKeys:
                    c += inSlot.meta.shape[inSlot.meta.axistags.index(flag)]
                else:
                    c += 1
                self.intervals.append((old_c, c))

        if len(self.inputs["Images"]) > 0:
            newshape = list(self.inputs["Images"][0].meta.shape)
            if flag in inTagKeys:
                #here we assume that all axis are present
                axisindex = self.Output.meta.axistags.index(flag)
                newshape[axisindex]=c
            else:
                newshape.insert(axisindex, c)
            self.outputs["Output"].meta.shape=tuple(newshape)
        else:
            self.outputs["Output"].meta.shape = None

    def execute(self, slot, subindex, rroi, result):
        key = roiToSlice(rroi.start,rroi.stop)

        cnt = 0
        written = 0
        start, stop = roi.sliceToRoi(key, self.outputs["Output"].meta.shape)
        assert (stop<=self.outputs["Output"].meta.shape).all()
        #axisindex = self.inputs["AxisIndex"].value
        flag = self.inputs["AxisFlag"].value
        axisindex = self.outputs["Output"].meta.axistags.index(flag)
        #ugly-ugly-ugly
        oldkey = list(key)
        oldkey.pop(axisindex)
        
        #print "STACKER: ", flag, axisindex
        #print "requesting an outslot from stacker:", key, result.shape
        #print "input slots total: ", len(self.inputs['Images'])
        requests = []


        for i, inSlot in enumerate(self.inputs['Images']):
            if inSlot.connected():
                req = None
                inTagKeys = [ax.key for ax in inSlot.meta.axistags]
                if flag in inTagKeys:
                    slices = inSlot.meta.shape[axisindex]
                    if cnt + slices >= start[axisindex] and start[axisindex]-cnt<slices and start[axisindex]+written<stop[axisindex]:
                        begin = 0
                        if cnt < start[axisindex]:
                            begin = start[axisindex] - cnt
                        end = slices
                        if cnt + end > stop[axisindex]:
                            end -= cnt + end - stop[axisindex]
                        key_ = copy.copy(oldkey)
                        key_.insert(axisindex, slice(begin, end, None))
                        reskey = [slice(None, None, None) for x in range(len(result.shape))]
                        reskey[axisindex] = slice(written, written+end-begin, None)

                        req = inSlot[tuple(key_)].writeInto(result[tuple(reskey)])
                        written += end - begin
                    cnt += slices
                else:
                    if cnt>=start[axisindex] and start[axisindex] + written < stop[axisindex]:
                        #print "key: ", key, "reskey: ", reskey, "oldkey: ", oldkey
                        #print "result: ", result.shape, "inslot:", inSlot.meta.shape
                        reskey = [slice(None, None, None) for s in oldkey]
                        reskey.insert(axisindex, written)
                        destArea = result[tuple(reskey)]
                        req = inSlot[tuple(oldkey)].writeInto(destArea)
                        written += 1
                    cnt += 1

                if req is not None:
                    requests.append(req)

        for r in requests:
            r.wait()

    def propagateDirty(self, inputSlot, subindex, roi):
        if not self.Output.ready():
            # If we aren't even fully configured, there's no need to notify downstream slots about dirtiness
            return
        if inputSlot == self.AxisFlag or inputSlot == self.AxisIndex:
            self.Output.setDirty( slice(None) )

        elif inputSlot == self.Images:
            imageIndex = subindex[0]
            axisflag = self.AxisFlag.value
            axisIndex = self.Output.meta.axistags.index(axisflag)
            if len(roi.start)>axisIndex:
                roi.start[axisIndex] += self.intervals[imageIndex][0] 
                roi.stop[axisIndex] += self.intervals[imageIndex][0] 
                self.Output.setDirty( roi )
            else:
                newroi = copy.copy(roi)
                newroi = newroi.insertDim(axisIndex, self.intervals[imageIndex][0], self.intervals[imageIndex][0]+1)
                self.Output.setDirty( newroi )
                
        else:
            assert False, "Unknown input slot."


class OpSingleChannelSelector(Operator):
    name = "SingleChannelSelector"
    description = "Select One channel from a Multichannel Image"

    inputSlots = [InputSlot("Input"),InputSlot("Index",stype='integer')]
    outputSlots = [OutputSlot("Output")]

    def setupOutputs(self):
        
        indexAxis=self.inputs["Input"].meta.axistags.channelIndex
        inshape=list(self.inputs["Input"].meta.shape)
        outshape = list(inshape)
        outshape.pop(indexAxis)
        outshape.insert(indexAxis, 1)
        outshape=tuple(outshape)

        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.shape = outshape

        # Output can't be accessed unless the input has enough channels
        if self.Input.meta.getTaggedShape()['c'] <= self.Index.value:
            self.Output.meta.NOTREADY = True
        
    def execute(self, slot, subindex, roi, result):
        index=self.inputs["Index"].value
        channelIndex = self.Input.meta.axistags.channelIndex
        assert self.inputs["Input"].meta.shape[channelIndex] > index, \
            "Requested channel, {}, is out of Range (input shape is {})".format( index, self.Input.meta.shape )

        # Only ask for the channel we need
        key = roiToSlice(roi.start,roi.stop)
        newKey = list(key)
        newKey[channelIndex] = slice(index, index+1, None)
        #newKey = key[:-1] + (slice(index,index+1),)
        self.inputs["Input"][tuple(newKey)].writeInto(result).wait()
        return result

    def propagateDirty(self, slot, subindex, roi):
        key = roi.toSlice()
        if slot == self.Input:
            channelIndex = self.Input.meta.axistags.channelIndex
            newKey = list(key)
            newKey[channelIndex] = slice(0, 1, None)
            #key = key[:-1] + (slice(0,1,None),)
            self.outputs["Output"].setDirty(tuple(newKey))
        else:
            self.Output.setDirty(slice(None))


class OpSubRegion(Operator):
    name = "OpSubRegion"
    description = "Select a region of interest from an numpy array"

    inputSlots = [InputSlot("Input"), InputSlot("Start"), InputSlot("Stop"), InputSlot("propagate_dirty", value = True)]
    outputSlots = [OutputSlot("Output")]
    
    def __init__(self, *args, **kwargs):
        Operator.__init__(self, *args, **kwargs)
        self._propagate_dirty = False

    def setupOutputs(self):
        self._propagate_dirty = self.propagate_dirty.value
        start = self.inputs["Start"].value
        stop = self.inputs["Stop"].value
        assert isinstance(start, tuple)
        assert isinstance(stop, tuple)
        assert len(start) == len(self.inputs["Input"].meta.shape)
        assert len(start) == len(stop)
        assert (numpy.array(stop)>= numpy.array(start)).all()
    
        temp = tuple(numpy.array(stop) - numpy.array(start))
        #drop singleton dimensions
        outShape = ()
        for e in temp:
            if e > 0:
                outShape = outShape + (e,)

        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.shape = outShape
        if self.Input.meta.drange is not None:
            self.Output.meta.drange = self.Input.meta.drange

    def execute(self, slot, subindex, roi, result):
        key = roiToSlice(roi.start,roi.stop)

        start = self.inputs["Start"].value
        stop = self.inputs["Stop"].value

        temp = tuple()
        for i in xrange(len(start)):
            if stop[i] - start[i] > 0:
                temp += (stop[i]-start[i],)

        readStart, readStop = sliceToRoi(key, temp)
        newKey = ()
        i = 0
        i2 = 0
        for kkk in xrange(len(start)):
            e = stop[kkk] - start[kkk]
            if e > 0:
                newKey += (slice(start[i2] + readStart[i], start[i2] + readStop[i],None),)
                i +=1
            else:
                newKey += (slice(start[i2], start[i2], None),)
            i2 += 1
        self.inputs["Input"][newKey].writeInto(result).wait()
        return result
        
    def propagateDirty(self, dirtySlot, subindex, roi):
        if self._propagate_dirty and dirtySlot == self.Input:
            # Translate the input key to a small subregion key
            smallstart = roi.start - self.Start.value
            smallstop = roi.stop - self.Start.value
            
            # Clip to our output shape
            smallstart = numpy.maximum(smallstart, 0)
            smallstop = numpy.minimum(smallstop, self.Output.meta.shape)

            # If there's an intersection with our output,
            #  propagate dirty region to output
            if ((smallstop - smallstart ) > 0).all():
                self.Output.setDirty( smallstart, smallstop )


class OpMultiArrayMerger(Operator):
    inputSlots = [InputSlot("Inputs", level=1),InputSlot('MergingFunction')]
    outputSlots = [OutputSlot("Output")]

    name = "Merge Multi Arrays based on a variadic merging function"
    category = "Misc"

    def setupOutputs(self):
        shape=self.inputs["Inputs"][0].meta.shape
        axistags=copy.copy(self.inputs["Inputs"][0].meta.axistags)

        self.outputs["Output"].meta.shape = shape
        self.outputs["Output"].meta.axistags = axistags
        self.outputs["Output"].meta.dtype = self.inputs["Inputs"][0].meta.dtype

        for input in self.inputs["Inputs"]:
            assert input.meta.shape==shape, "Only possible merging consistent shapes"
            assert input.meta.axistags==axistags, "Only possible merging same axistags"

        # If *all* inputs have a drange, then provide a drange for the output.
        # Note: This assumes the merging function is pixel-wise
        dranges = []
        for i,slot in enumerate(self.Inputs):
            dr = slot.meta.drange
            if dr is not None:
                dranges.append(numpy.array(dr))
            else:
                dranges = []
                break
        
        if len(dranges) > 0:
            fun = self.MergingFunction.value
            outRange = fun(dranges)
            self.Output.meta.drange = tuple(outRange)

    def execute(self, slot, subindex, roi, result):
        key = roiToSlice(roi.start,roi.stop)
        requests=[]
        for input in self.inputs["Inputs"]:
            requests.append(input[key])

        data=[]
        for req in requests:
            data.append(req.wait())
        
        fun=self.inputs["MergingFunction"].value

        return fun(data)

    def propagateDirty(self, dirtySlot, subindex, roi):
        if dirtySlot == self.MergingFunction:
            self.Output.setDirty( slice(None) )
        elif dirtySlot == self.Inputs:
            # Assumes a pixel-wise merge function.
            key = roi.toSlice()
            self.Output.setDirty( key )


class OpMaxChannelIndicatorOperator(Operator):
    name = "OpMaxChannelIndicatorOperator"

    Input = InputSlot()
    Output = OutputSlot()

    def setupOutputs(self):
        self.Output.meta.shape = self.Input.meta.shape
        self.Output.meta.axistags = self.Input.meta.axistags
        self.Output.meta.dtype = numpy.uint8
        self._num_channels = self.Input.meta.shape[-1]

    def execute(self, slot, subindex, roi, result):
        key = roi.toSlice()
        data = self.inputs["Input"][key[:-1]+(slice(None),)].wait()
    
        #FIXME: only works if channels are in last dimension
        dm = numpy.max(data, axis = data.ndim-1)
        res = numpy.zeros(data.shape, numpy.uint8)

        for c in range(data.shape[-1]):
            res[...,c] = numpy.where(data[...,c] == dm, 1, 0)    

        result[:] = res[...,key[-1]]

    def propagateDirty(self, slot, subindex, roi):
        key = roi.toSlice()
        if slot == self.Input:
            self.outputs["Output"].setDirty(key)


class OpPixelOperator(Operator):
    name = "OpPixelOperator"
    description = "simple pixel operations"

    inputSlots = [InputSlot("Input"), InputSlot("Function")]
    outputSlots = [OutputSlot("Output")]

    def setupOutputs(self):
        self.function = self.inputs["Function"].value

        self.Output.meta.shape = self.Input.meta.shape
        self.Output.meta.axistags = self.Input.meta.axistags
        
        # To determine the output dtype, we'll test the function on a tiny array.
        # For pathological functions, this might raise an exception (e.g. divide by zero).
        testInputData = numpy.array([1], dtype=self.Input.meta.dtype)
        self.Output.meta.dtype = self.function( testInputData ).dtype.type
        
        # Provide a default drange.
        # Works for monotonic functions.
        drange_in = self.Input.meta.drange
        if drange_in is not None:
            drange_out = self.function( numpy.array(drange_in) )
            self.Output.meta.drange = tuple(drange_out)

    def execute(self, slot, subindex, roi, result):
        key = roiToSlice(roi.start,roi.stop)

        req = self.inputs["Input"][key]
        # Re-use the result array as a temporary variable (if possible)
        if self.Input.meta.dtype == self.Output.meta.dtype:
            req.writeInto(result)
        matrix = req.wait()
        result[:] = self.function(matrix)
        return result

    def propagateDirty(self, slot, subindex, roi):
        key = roi.toSlice()
        if slot == self.Input:
            self.outputs["Output"].setDirty(key)
        elif slot == self.Function:
            self.Output.setDirty( slice(None) )
        else:
            assert False, "Unknown dirty input slot"

    @property
    def shape(self):
        return self.outputs["Output"].meta.shape

    @property
    def dtype(self):
        return self.outputs["Output"].meta.dtype


class OpMultiInputConcatenater(Operator):
    name = "OpMultiInputConcatenater"
    description = "Combine two or more MultiInput slots into a single MultiOutput slot"

    Inputs = InputSlot(level=2, optional=True)
    Output = OutputSlot(level=1)

    def __init__(self, *args, **kwargs):
        super(OpMultiInputConcatenater, self).__init__(*args, **kwargs)
        self._numInputLists = 0

    def getOutputIndex(self, inputMultiSlot, inputIndex):
        """
        Determine which output index corresponds to the given input multislot and index.
        """
        # Determine the corresponding output index
        outputIndex = 0
        # Search for the input slot
        for index, multislot in enumerate( self.Inputs ):
            if inputMultiSlot != multislot:
                # Not the resized slot.  Skip all its subslots
                outputIndex += len(multislot)
            else:
                # Found the resized slot.  Add the offset and stop here.
                outputIndex += inputIndex
                return outputIndex

        assert False

    def handleInputInserted(self, resizedSlot, inputPosition, totalsize):
        """
        A slot was inserted in one of our inputs.
        Insert a slot in the appropriate location of our output, and connect it to the appropriate input subslot.
        """
        # Determine which output slot this corresponds to
        outputIndex = self.getOutputIndex(resizedSlot, inputPosition)

        # Insert new output slot and connect it up.
        newOutputLength = len( self.Output ) + 1
        self.Output.insertSlot(outputIndex, newOutputLength)
        self.Output[outputIndex].connect( resizedSlot[inputPosition] )

    def handleInputRemoved(self, resizedSlot, inputPosition, totalsize):
        """
        A slot was removed from one of our inputs.
        Remove the appropriate slot from our output.
        """
        # Determine which output slot this corresponds to
        outputIndex = self.getOutputIndex(resizedSlot, inputPosition)

        # Remove the corresponding output slot
        newOutputLength = len( self.Output ) - 1
        self.Output.removeSlot(outputIndex, newOutputLength)

    def setupOutputs(self):
        # This function is merely provided to initialize ourselves if one of our input lists was set up in advance.
        # We don't need to do this expensive rebuilding of the output list unless a new input list was added
        if self._numInputLists == len(self.Inputs):
            return
        
        self._numInputLists = len(self.Inputs)
            
        # First pass to determine output length
        totalOutputLength = 0
        for index, slot in enumerate( self.Inputs ):
            totalOutputLength += len(slot)

        self.Output.resize( totalOutputLength )

        # Second pass to make connections and subscribe to future changes
        outputIndex = 0
        for index, slot in enumerate( self.Inputs ):
            slot.notifyInserted( self.handleInputInserted )
            slot.notifyRemove( self.handleInputRemoved )

            # Connect subslots to output
            for i, s in enumerate(slot):
                self.Output[outputIndex].connect(s)
                outputIndex += 1


    def execute(self, slot, subindex, roi, result):
        # Should never be called.  All output slots are directly connected to an input slot.
        assert False

    def propagateDirty(self, inputSlot, subindex, roi):
        # Nothing to do here.
        # All outputs are directly connected to an input slot.
        pass

        
class OpWrapSlot(Operator):
    """
    Adaptor for when you have a slot and you need to make it look like a multi-slot.
    Converts a single slot into a multi-slot of len == 1 and level == 1
    """
    Input = InputSlot()
    Output = OutputSlot(level=1)

    def __init__(self, *args, **kwargs):
        super(OpWrapSlot, self).__init__(*args, **kwargs)
        self.Output.resize(1)
        self.Output[0].connect( self.Input )

    def setupOutputs(self):
        pass

    def execute(self, slot, subindex, roi, result):
        assert False

    def propagateDirty(self, inputSlot, subindex, roi):
        pass

    def setInSlot(self, slot, subindex, roi, value):
        self.Output[0][roi.toSlice()] = value


class OpTransposeSlots(Operator):
    """
    Takes an input slot indexed as [i][j] and produces an output slot indexed as [j][i]
    Note: Only works for a slot of level 2.
    
    This operator is designed to be usable even if the inputs are only partially 
    configured (e.g. if not all input multi-slots have the same length).
    The length of the output multi-slot must be specified explicitly.
    """
    OutputLength = InputSlot() # The length of the j dimension = J.
    Inputs = InputSlot(level=2, optional=True) # Optional so that the Output mslot is configured even if len(Inputs) == 0
    Outputs = OutputSlot(level=2)   # A level-2 multislot of len J.
                                    # For each inner (level-1) multislot, len(multislot) == len(self.Inputs)

    _Dummy = InputSlot(optional=True) # Internal use only.  Do not connect.

    def __init__(self, *args, **kwargs):
        super( OpTransposeSlots, self ).__init__(*args, **kwargs)

    def setupOutputs(self):
        self.Outputs.resize( self.OutputLength.value )
        for j, mslot in enumerate( self.Outputs ):
            mslot.resize( len(self.Inputs) )
            for i, oslot in enumerate( mslot ):
                if i < len(self.Inputs) and j < len(self.Inputs[i]):
                    oslot.connect( self.Inputs[i][j] )
                else:
                    # Ensure that this output is NOT ready.
                    oslot.connect( self._Dummy )

    def execute(self, slot, subindex, roi, result):
        # Should never be called.  All output slots are directly connected to an input slot.
        assert False

    def propagateDirty(self, inputSlot, subindex, roi):
        # Nothing to do here.
        # All outputs are directly connected to an input slot.
        pass

class OpDtypeView(Operator):
    """
    Connect an input slot of one dtype to an output with a different
     (but compatible) dtype, WITHOUT creating a copy.
    For example, convert uint32 to int32.
    
    Note: This operator uses ndarray.view() and must be used with care.
          For example, don't use it to convert a float to an int (or vice-versa), 
             and don't use it to convert e.g. uint8 to uint32.
          See ndarray.view() documentation for details.
    """
    Input = InputSlot()
    OutputDtype = InputSlot()
    
    Output = OutputSlot()
    
    def setupOutputs(self):
        self.Output.meta.assignFrom( self.Input.meta )
        self.Output.meta.dtype = self.OutputDtype.value
        #self.Output.meta.dtype = numpy.uint32

    def execute(self, slot, subindex, roi, result):
        result_view = result.view( self.Input.meta.dtype )
        self.Input(roi.start, roi.stop).writeInto( result_view ).wait()
        return result

    def propagateDirty(self, slot, subindex, roi):
        self.Output.setDirty( roi )















    