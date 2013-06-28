import logging
from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow.operators.adaptors import Op5ifyer
from lazyflow.stype import Opaque
from lazyflow.rtype import SubRegion
import numpy as np
import vigra

import cPickle as pickle

np.set_printoptions(linewidth=200)

############################
############################           
############################
###                      ###
###  Top Level Operator  ###
###                      ###
############################
############################
############################

loggerName = __name__ 
logger = logging.getLogger(loggerName)


class OpInterpMissingData(Operator):
    name = "OpInterpMissingData"

    InputVolume = InputSlot()
    InputSearchDepth = InputSlot(value=3)
    PatchSize = InputSlot(value=64)
    HaloSize = InputSlot(value=0)
    DetectionMethod = InputSlot(value='svm')
    InterpolationMethod = InputSlot(value='cubic')
      
    Output = OutputSlot()
    Missing = OutputSlot()
    
    _requiredMargin = {'cubic': 2, 'linear': 1, 'constant': 0}
    
    def __init__(self, *args, **kwargs):
        super(OpInterpMissingData, self).__init__(*args, **kwargs)
        
        
        self.detector = OpDetectMissing(parent=self)
        self.interpolator = OpInterpolate(parent=self)
        
        self.detector.InputVolume.connect(self.InputVolume)
        self.detector.PatchSize.connect(self.PatchSize)
        self.detector.HaloSize.connect(self.HaloSize)
        self.detector.DetectionMethod.connect(self.DetectionMethod)
        
        self.interpolator.InputVolume.connect(self.InputVolume)
        self.interpolator.Missing.connect(self.detector.Output) 
        self.interpolator.InterpolationMethod.connect(self.InterpolationMethod) 
        
        self.Missing.connect(self.detector.Output)

    def dumps(self):
        #FIXME this is not good
        #       a) accessing private attribute
        #       b) could be bad if sklearn becomes available after saving
        return self.detector.dumps()
    
    def loads(self, s):
        self.detector.loads(s)

    def setupOutputs(self):
        # Output has the same shape/axes/dtype/drange as input
        self.Output.meta.assignFrom( self.InputVolume.meta )

        # Check for errors
        taggedShape = self.InputVolume.meta.getTaggedShape()
        
        # this assumption is important!
        if 't' in taggedShape:
            assert taggedShape['t'] == 1, "Non-spatial dimensions must be of length 1"
        if 'c' in taggedShape:
            assert taggedShape['c'] == 1, "Non-spatial dimensions must be of length 1"

    def execute(self, slot, subindex, roi, result):
        '''
        execute
        '''
        
        method = self.InterpolationMethod.value
        
        assert method in self._requiredMargin.keys(), "Unknown interpolation method {}".format(method)
        
        def roi2slice(roi):
            out = []
            for start, stop in zip(roi.start, roi.stop):
                out.append(slice(start, stop))
            return tuple(out)

        
        oldStart = np.asarray([k for k in roi.start])
        oldStop = np.asarray([k for k in roi.stop])

        z_index = self.InputVolume.meta.axistags.index('z')
        nz = self.InputVolume.meta.getTaggedShape()['z']
        
        # check if more input is needed, and how many
        z_offsets = self._extendRoi(roi)
        
        
        # get extended interpolation
        roi.start[z_index] -= z_offsets[0]
        roi.stop[z_index] += z_offsets[1]
        
        a = self.interpolator.Output.get(roi).wait()
        
        # reduce to original roi
        roi.stop = roi.stop - roi.start
        roi.start *= 0
        roi.start[z_index] += z_offsets[0]
        roi.stop[z_index] -= z_offsets[1]
        key = roi2slice(roi)
        
        result[:] = a[key]
        
        
        return result
    
    
    def propagateDirty(self, slot, subindex, roi):
        # TODO: This implementation of propagateDirty() isn't correct.
        #       That's okay for now, since this operator will never be used with input data that becomes dirty.
        #TODO if the input changes we're doing nothing?
        #logger.warning("FIXME: propagateDirty not implemented!")
        self.Output.setDirty(roi)
        
    
    def _extendRoi(self, roi):
        
        
        origStart = np.copy(roi.start)
        origStop = np.copy(roi.stop)
        
        
        offset_top = 0
        offset_bot = 0
        
        z_index = self.InputVolume.meta.axistags.index('z')
        
        depth = self.InputSearchDepth.value
        nRequestedSlices = roi.stop[z_index] - roi.start[z_index]
        nNeededSlices = self._requiredMargin[self.InterpolationMethod.value]
        
        missing = vigra.taggedView(self.detector.Output.get(roi).wait(), axistags=self.InputVolume.meta.axistags).withAxes(*'zyx')
        
        nGoodSlicesTop = 0
        # go inside the roi
        for k in range(nRequestedSlices):
            if np.all(missing[k,...]==0): #clean slice
                nGoodSlicesTop += 1
            else:
                break
        
        # are we finished yet?
        if nGoodSlicesTop >= nRequestedSlices:
            return (0,0)
        
        # looks like we need more slices on top
        while nGoodSlicesTop < nNeededSlices and offset_top < depth and roi.start[z_index] > 0:
            roi.stop[z_index] = roi.start[z_index] 
            roi.start[z_index] -= 1
            offset_top += 1
            topmissing = self.detector.Output.get(roi).wait()
            if np.all(topmissing==0): # clean slice
                nGoodSlicesTop += 1
            else: # need to start again
                nGoodSlicesTop = 0
        
        
        nGoodSlicesBot = 0
        # go inside the roi
        for k in range(1,nRequestedSlices+1):
            if np.all(missing[-k,...]==0): #clean slice
                nGoodSlicesBot += 1
            else:
                break
        
        
        roi.start = np.copy(origStart)
        roi.stop = np.copy(origStop)
        
        
        # looks like we need more slices on bottom
        while nGoodSlicesBot < nNeededSlices and offset_bot < depth and roi.stop[z_index] < self.InputVolume.meta.getTaggedShape()['z']:
            roi.start[z_index] = roi.stop[z_index] 
            roi.stop[z_index] += 1
            offset_bot += 1
            botmissing = self.detector.Output.get(roi).wait()
            if np.all(botmissing==0): # clean slice
                nGoodSlicesBot += 1
            else: # need to start again
                nGoodSlicesBot = 0
                
        
        roi.start = np.copy(origStart)
        roi.stop = np.copy(origStop)
        
        return (offset_top,offset_bot)
        
        



################################
################################           
################################
###                          ###
###  Interpolation Operator  ###
###                          ###
################################
################################
################################

from vigra.analysis import labelVolumeWithBackground

def _cubic_mat(n=1):
    n = float(n)
    x = -1/(n+1)
    y = (n+2)/(n+1)
    
    A = [[1, x, x**2, x**3],\
        [1, 0, 0, 0],\
        [1, 1, 1, 1],\
        [1, y, y**2, y**3]]
    
    #TODO we could implement the direct inverse here, but it's a bit lengthy
    # compare to http://www.wolframalpha.com/input/?i=invert+matrix%28[1%2Cx%2Cx^2%2Cx^3]%2C[1%2C0%2C0%2C0]%2C[1%2C1%2C1%2C1]%2C[1%2Cy%2Cy^2%2Cy^3]%29
    
    return np.linalg.inv(A)



class OpInterpolate(Operator):
    InputVolume = InputSlot()
    Missing = InputSlot()
    InterpolationMethod = InputSlot(value='cubic')
    
    Output = OutputSlot()
    
    _requiredMargin = {'cubic': 2, 'linear': 1, 'constant': 0}
    _fallbacks = {'cubic': 'linear', 'linear': 'constant', 'constant': None}
    
    def propagateDirty(self, slot, subindex, roi):
        # TODO
        self.Output.setDirty(roi)
    
    def setupOutputs(self):
        # Output has the same shape/axes/dtype/drange as input
        self.Output.meta.assignFrom( self.InputVolume.meta )
        
        try:
            self._iinfo = np.iinfo(self.InputVolume.meta.dtype)
        except ValueError:
            # not integer type, no casting needed
            self._iinfo = None

        assert self.InputVolume.meta.getTaggedShape() == self.Missing.meta.getTaggedShape(), \
                "InputVolume and Missing must have the same shape ({} vs {})".format(\
                self.InputVolume.meta.getTaggedShape(), self.Missing.meta.getTaggedShape())
            
        

    def execute(self, slot, subindex, roi, result):
        
        # prefill result
        result[:] = self.InputVolume.get(roi).wait()
        
        resultZYXCT = vigra.taggedView(result,self.InputVolume.meta.axistags).withAxes(*'zyxct')
        missingZYXCT = vigra.taggedView(self.Missing.get(roi).wait(),self.Missing.meta.axistags).withAxes(*'zyxct')
                
        for t in range(resultZYXCT.shape[4]):
            for c in range(resultZYXCT.shape[3]):
                missingLabeled = labelVolumeWithBackground(missingZYXCT[...,c,t])
                maxLabel = missingLabeled.max()
                for i in range(1,maxLabel+1):
                    self._interpolate(resultZYXCT[...,c,t], missingLabeled==i)
        
        
        
        return result
    
    def _cast(self, x):
        '''
        casts the array to expected range (i.e. 0..255 for uint8 types, ...)
        '''
        if not self._iinfo is None:
            x = np.where(x>self._iinfo.max, self._iinfo.max, x)
            x = np.where(x<self._iinfo.min, self._iinfo.min, x)
        return x
        
        
    def _interpolate(self,volume, missing, method = None):
        '''
        interpolates in z direction
        :param volume: 3d block with axistags 'zyx'
        :type volume: array-like
        :param missing: True where data is missing
        :type missing: bool, 3d block with axistags 'zyx'
        :param method: 'cubic' or 'linear' or 'constant' (see class documentation)
        :type method: str
        '''
        
        method = self.InterpolationMethod.value if method is None else method
        # sanity checks
        assert method in self._requiredMargin.keys(), "Unknown method '{}'".format(method)

        assert volume.axistags.index('z') == 0 \
            and volume.axistags.index('y') == 1 \
            and volume.axistags.index('x') == 2 \
            and len(volume.shape) == 3, \
            "Data must be 3d with z as first axis."
        
        # number and z-location of missing slices (z-axis is at zero)
        black_z_ind, black_y_ind, black_x_ind = np.where(missing)
        
        
        if len(black_z_ind) == 0: # no need for interpolation
            return 
        
        # indices with respect to the required margin around the missing values
        minZ = black_z_ind.min() - self._requiredMargin[method]
        maxZ = black_z_ind.max() + self._requiredMargin[method]
        
        
        n = maxZ-minZ-2*self._requiredMargin[method]+1
        
        if not (minZ>-1 and maxZ < volume.shape[0]):
            # this method is not applicable, try another one
            logger.warning("Margin not big enough for interpolation (need at least {} pixels for '{}')".format(self._requiredMargin[method], method))
            if self._fallbacks[method] is not None:
                logger.warning("Falling back to method '{}'".format(self._fallbacks[method]))
                self._interpolate(volume, missing, self._fallbacks[method])
                return
            else:
                assert False, "Margin not big enough for interpolation (need at least {} pixels for '{}') and no fallback available".format(self._requiredMargin[method], method)
                
        minY, maxY = (black_y_ind.min(), black_y_ind.max())
        minX, maxX = (black_x_ind.min(), black_x_ind.max())
        
        if method == 'linear':
            # do a convex combination of the slices to the left and to the right
            xs = np.linspace(0,1,n+2)
            left = volume[minZ,minY:maxY+1,minX:maxX+1]
            right = volume[maxZ,minY:maxY+1,minX:maxX+1]

            for i in range(n):
                # interpolate every slice
                volume[minZ+i+1,minY:maxY+1,minX:maxX+1] =  self._cast((1-xs[i+1])*left + xs[i+1]*right)
                
        elif method == 'cubic': 
            # interpolation coefficients
            
            D = np.rollaxis(volume[[minZ,minZ+1,maxZ-1,maxZ],minY:maxY+1,minX:maxX+1],0,3)
            F = np.tensordot(D,_cubic_mat(n),([2,],[1,]))
            
            xs = np.linspace(0,1,n+2)
            for i in range(n):
                # interpolate every slice
                x = xs[i+1]
                volume[minZ+i+2,minY:maxY+1,minX:maxX+1] = self._cast(F[...,0] + F[...,1]*x + F[...,2]*x**2 + F[...,3]*x**3 )
                
        else: #constant
            if minZ > 0:
                # fill right hand side with last good slice
                for i in range(maxZ-minZ+1):
                    volume[minZ+i,minY:maxY+1,minX:maxX+1] = volume[minZ-1,minY:maxY+1,minX:maxX+1]
            elif maxZ < volume.shape[0]-1:
                # fill left hand side with last good slice
                for i in range(maxZ-minZ+1):
                    volume[minZ+i,minY:maxY+1,minX:maxX+1] = volume[maxZ+1,minY:maxY+1,minX:maxX+1]
            else:
                # nothing to do for empty block
                logger.warning("Not enough data for interpolation, leaving slice as is ...")
            
            
############################
############################           
############################
###                      ###
###  Detection Operator  ###
###                      ###
############################
############################
############################

from lazyflow.operators.opPatchCreator import patchify

try:
    from sklearn.svm import SVC
    havesklearn = True
except ImportError:
    logger.warning("Could not import dependency 'sklearn' for SVMs")
    havesklearn = False
    
class PseudoSVC(object):
    '''
    pseudo SVM 
    '''
    
    def __init__(self, *args, **kwargs):
        pass
    
    def fit(self, *args, **kwargs):
        pass
        
    def predict(self,*args, **kwargs):
        X = args[0]
        out = np.zeros(len(X))
        for k, patch in enumerate(X):
            out[k] = 1 if np.all(patch[1:] == 0) else 0
        return out
            

def _histogramIntersectionKernel(X,Y):
    '''
    implements the histogram intersection kernel in a fancy way
    (standard: k(x,y) = sum(min(x_i,y_i)) )
    '''
    A = X.reshape( (X.shape[0],1,X.shape[1]) )
    B = Y.reshape( (1,) + Y.shape )
    return np.sum(A+B-np.abs(A-B), axis=2)

def _defaultTrainingSet(defectSize=128):
    '''
    produce a standard training set with black regions
    '''
    vol = vigra.VigraArray(((np.random.rand(200,200,50)-.5)*125+125).astype(np.uint8), axistags=vigra.defaultAxistags('xyz'))
    labels = vigra.VigraArray(np.zeros((200,200,50),dtype=np.uint8), axistags=vigra.defaultAxistags('xyz'))
    
    labels[70:70+defectSize,70:70+defectSize,1:3] = 2
    labels[70:70+defectSize,70:70+defectSize,5:7] = 1
    vol[70:70+defectSize,70:70+defectSize,5:7] = 0
    return (vol, labels)


class OpDetectMissing(Operator):
    '''
    Sub-Operator for detection of missing image content
    '''
    
    InputVolume = InputSlot()
    PatchSize = InputSlot(value=32)
    HaloSize = InputSlot(value=32)
    DetectionMethod = InputSlot(value='classic')
    NHistogramBins = InputSlot(value=20)
    
    TrainingVolume = InputSlot(value = _defaultTrainingSet()[0])
    
    # labels: {0: unknown, 1: missing, 2: good}
    TrainingLabels = InputSlot(value = _defaultTrainingSet()[1])
    NTrainingSamples = InputSlot(value=250)
    
    Output = OutputSlot()
    
    
    
    ### PRIVATE ###
    _detectors = {'svm': {}, 'classic': {}}
    
    
    def __init__(self, *args, **kwargs):
        super(OpDetectMissing, self).__init__(*args, **kwargs)
        
        
    def propagateDirty(self, slot, subindex, roi):
        if slot == self.InputVolume:
            #FIXME what about change of patch size and so on?
            self.Output.setDirty(roi)
    
    
    def setupOutputs(self):
        self.Output.meta.assignFrom( self.InputVolume.meta )
        self.Output.meta.dtype = np.uint8
        
        tags = self.TrainingVolume.meta.getTaggedShape()
        if 't' in tags: assert tags['t'] == 1, "Time axis must be singleton"
        if 'c' in tags: assert tags['c'] == 1, "Channel axis must be singleton"
        assert self.TrainingVolume.meta.getTaggedShape() == self.TrainingLabels.meta.getTaggedShape(), \
            "Training labels and training volume must have the same shape."  
        
        # determine range of input
        if self.InputVolume.meta.dtype == np.uint8:
            r = (0,255) 
        elif self.InputVolume.meta.dtype == np.uint16:
            r = (0,65535) 
        else:
            #FIXME hardcoded range, use np.iinfo
            r = (0,255)
        self._inputRange = r


    def execute(self, slot, subindex, roi, result):
        
        # sanity check
        assert self.DetectionMethod.value in ['svm', 'classic'], \
            "Unknown detection method '{}'".format(self.DetectionMethod.value)
        
        # prefill result
        result[:] = 0
        resultZYXCT = vigra.taggedView(result,self.InputVolume.meta.axistags).withAxes(*'zyxct')
            
        # acquire data
        data = self.InputVolume.get(roi).wait()
        dataZYXCT = vigra.taggedView(data,self.InputVolume.meta.axistags).withAxes(*'zyxct')
        
        # walk over time and channel axes
        for t in range(dataZYXCT.shape[4]):
            for c in range(dataZYXCT.shape[3]):
                resultZYXCT[...,c,t] = self._detectMissing(slot, dataZYXCT[...,c,t])

        return result
    
    
    def dumps(self):
        return pickle.dumps(self._detectors)
    
    
    def loads(self, s):
        self._detectors = pickle.loads(s)
    

    def _detectMissing(self, slot, data):
        '''
        detects missing regions and labels each missing region with 
        its own integer value
        :param origData: 3d data 
        :type origData: array-like
        '''
        
        assert data.axistags.index('z') == 0 \
            and data.axistags.index('y') == 1 \
            and data.axistags.index('x') == 2 \
            and len(data.shape) == 3, \
            "Data must be 3d with axis 'zyx'."
        
        self._train(force=False)
        
        result = vigra.VigraArray(data)*0
        
        patchSize = self.PatchSize.value
        haloSize = self.HaloSize.value
        
        if patchSize is None or not patchSize>0:
            raise ValueError("PatchSize must be a positive integer")
        if haloSize is None or haloSize<0:
            raise ValueError("HaloSize must be a non-negative integer")
        
        if np.any(patchSize>np.asarray(data.shape[1:])):
            logger.debug("Ignoring small region (shape={})".format(dict(zip([k.key for k in data.axistags], data.shape))))
            maxZ=0
        else:
            maxZ = data.shape[0]
            
        # choose detector to take
        currentDetector = self._detectors[self.DetectionMethod.value][patchSize**2]
            
        # walk over slices
        for z in range(maxZ):
            patches, positions = patchify(data[z,:,:].view(np.ndarray), (patchSize, patchSize), (haloSize,haloSize), (0,0), data.shape[1:])
            # walk over patches
            for patch, pos in zip(patches, positions):
                (hist, _) = np.histogram(patch, bins=self.NHistogramBins.value, range=self._inputRange)
                if currentDetector.predict((hist,))[0] > 0: 
                    #patch is classified as missing
                    ystart = pos[0]
                    ystop = min(ystart+patchSize, data.shape[1])
                    xstart = pos[1]
                    xstop = min(xstart+patchSize, data.shape[2])
                    result[z,ystart:ystop,xstart:xstop] = 1
         
        return result
        
        
    def _train(self, force=False):
        '''
        trains with samples drawn from slots TrainingVolume and TrainingLabels
        (retrains only if patch size is currently untrained or force is True)
        '''
        patchSize = self.PatchSize.value + self.HaloSize.value
        
        # return early if unneccessary
        if not force and patchSize**2 in self._detectors.keys():
            return
        
        logger.debug("Training for {} patch elements ...".format(patchSize**2))
        
        if self.DetectionMethod.value == 'svm' and havesklearn:
            self._detectors[self.DetectionMethod.value][patchSize**2] = SVC(kernel=_histogramIntersectionKernel)
        else:
            self._detectors[self.DetectionMethod.value][patchSize**2] = PseudoSVC(kernel=_histogramIntersectionKernel)
            return
        
        #from PyQt4.QtCore import pyqtRemoveInputHook
        #from pdb import set_trace
        #pyqtRemoveInputHook()
        #set_trace()
            
        vol = vigra.taggedView(self.TrainingVolume[:].wait(),axistags=self.TrainingVolume.meta.axistags).withAxes(*'zyx')
        labels = vigra.taggedView(self.TrainingLabels[:].wait(),axistags=self.TrainingLabels.meta.axistags).withAxes(*'zyx')
        
        #BEGIN subroutine
        def _extractHistograms(vol, cond, nPatches=10):
            
            filt = np.ones((patchSize, patchSize))
            out = []
            
            ind_z, ind_y, ind_x = np.where(cond.view(np.ndarray))
            
            choice = np.random.permutation(len(ind_x))
            
            for z,y,x in zip(ind_z[choice], ind_y[choice],ind_x[choice]):
                if len(out)>=nPatches:
                    break
                ymin = y - patchSize//2
                ymax = ymin + patchSize
                xmin = x - patchSize//2
                xmax = xmin + patchSize
                
                if not (xmin < 0 or ymin < 0 or xmax > vol.shape[2] or ymax > vol.shape[1]):
                    # valid patch, add it to the output
                    (hist, _) = np.histogram(vol[z,ymin:ymax,xmin:xmax], bins=self.NHistogramBins.value, range=self._inputRange)
                    out.append(hist)
                
            return out
        #END subroutine
        
        bad = _extractHistograms(vol, labels == 1, nPatches = self.NTrainingSamples.value)
        good = _extractHistograms(vol, labels == 2, nPatches = self.NTrainingSamples.value)
        
        
        if len(bad)== 0 or len(good) == 0:
            logger.error("Could not extract training data from volume - training aborted.")
            return
        
        labelGood = [0]*len(good)
        labelBad = [1]*len(bad)
        
        x = good + bad
        y = labelGood+labelBad
        self._detectors[self.DetectionMethod.value][patchSize**2].fit(x, y)
        

if __name__ == "__main__":
    pass
    
