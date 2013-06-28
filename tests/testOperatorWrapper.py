from lazyflow.graph import Graph, Operator, InputSlot, OutputSlot, OperatorWrapper
from lazyflow.operators import Op5ToMulti
import numpy
import copy

class OpSimple(Operator):
    InputA = InputSlot()
    InputB = InputSlot()
    Output = OutputSlot()

    def setupOutputs(self):
        self.Output.meta.shape = self.InputA.meta.shape
        self.Output.meta.dtype = self.InputA.meta.dtype

    def execute(self, slot, subindex, roi, result):
        assert slot == self.Output

        result[...] = self.InputA(roi.start, roi.stop).wait() * self.InputB[0:1].wait()

    def propagateDirty(self, inputSlot, subindex, roi):
        if inputSlot == self.InputA:
            self.Output.setDirty(roi)
        elif (inputSlot == self.InputB 
              and roi.start[0] == 0 
              and roi.stop[0] >= 1):
            dirtyRoi = copy.copy(roi)
            dirtyRoi.stop[0] = 1
            self.Output.setDirty(dirtyRoi)
        else:
            assert False

class OpExplicitMulti(Operator):
    Output = OutputSlot(level=1)
    
    def setupOutputs(self):
        pass

class OpCopyInput(Operator):
    Input = InputSlot()
    Output = OutputSlot()

    def setupOutputs(self):
        self.Output.setValue(self.Input.value)

    def propagateDirty(self, inputSlot, subindex, roi):
        self.Output.setDirty(roi)

class TestBasic(object):

    @classmethod
    def setupClass(cls):
        cls.graph = Graph()

    @classmethod
    def teardownClass(cls):
        pass

    def test_fullWrapping(self):
        """
        Test basic wrapping functionality (all slots are promoted)
        """
        wrapped = OperatorWrapper( OpSimple, graph=self.graph )
        assert type(wrapped.InputA) == InputSlot
        assert type(wrapped.InputB) == InputSlot
        assert type(wrapped.Output) == OutputSlot
        assert wrapped.InputA.level == 1
        assert wrapped.InputB.level == 1
        assert wrapped.Output.level == 1

        assert len(wrapped.InputA) == 0
        assert len(wrapped.InputB) == 0
        assert len(wrapped.Output) == 0

        wrapped.InputA.resize(2)
        assert len(wrapped.InputB) == 2
        assert len(wrapped.Output) == 2

        a = numpy.array([[1,2],[3,4]])
        b = numpy.array([2])
        wrapped.InputA[0].setValue(a)
        wrapped.InputB[0].setValue(b)
        wrapped.InputA[1].setValue(2*a)
        wrapped.InputB[1].setValue(3*b)

        result0 = wrapped.Output[0][0:2,0:2].wait()
        result1 = wrapped.Output[1][0:2,0:2].wait()
        assert ( result0 == a * b[0] ).all()
        assert ( result1 == 2*a * 3*b[0] ).all()

    def test_partialWrapping(self):
        """
        By default, OperatorWrapper promotes all slots.
        This function tests what happens when only a subset of the inputs are promoted.
        """
        wrapped = OperatorWrapper( OpSimple, graph=self.graph, promotedSlotNames=set(['InputA']) )
        assert type(wrapped.InputA) == InputSlot  
        assert type(wrapped.InputB) == InputSlot
        assert type(wrapped.Output) == OutputSlot 
        assert wrapped.InputA.level == 1 # Promoted because it was listed in the constructor call
        assert wrapped.InputB.level == 0 # NOT promoted
        assert wrapped.Output.level == 1 # Promoted because it's an output

        assert len(wrapped.InputA) == 0
        assert len(wrapped.InputB) == 0
        assert len(wrapped.Output) == 0

        wrapped.InputA.resize(2)
        assert len(wrapped.InputB) == 0 # Not promoted
        assert len(wrapped.Output) == 2

        a = numpy.array([[1,2],[3,4]])
        b = numpy.array([2])
        wrapped.InputA[0].setValue(a)
        wrapped.InputB.setValue(b)
        wrapped.InputA[1].setValue(2*a)

        result0 = wrapped.Output[0][0:2,0:2].wait()
        result1 = wrapped.Output[1][0:2,0:2].wait()
        assert ( result0 == a * b[0] ).all()
        assert ( result1 == 2*a * b[0] ).all()


class TestMultiOutputToWrapped(object):

    @classmethod
    def setupClass(cls):
        cls.graph = Graph()

    def test_input_output_resize(self):
        exMulti = OpExplicitMulti(graph=self.graph)

        wrappedSimple = OperatorWrapper( OpSimple, graph=self.graph )
        assert len(wrappedSimple.InputA) == 0

        wrappedSimple.InputA.connect( exMulti.Output )
        assert len(wrappedSimple.InputA) == 0

        exMulti.Output.resize( 1 )
        assert len(wrappedSimple.InputA) == 1
        assert len(wrappedSimple.InputB) == 1
        assert len(wrappedSimple.Output) == 1

    def test_setValues(self):
        wrappedCopier = OperatorWrapper( OpCopyInput, graph=self.graph )

        values = ["Subslot One", "Subslot Two"]
        wrappedCopier.Input.setValues( values )

        assert wrappedCopier.Output[0].value == values[0]
        assert wrappedCopier.Output[1].value == values[1]

if __name__ == "__main__":
    import nose
    ret = nose.run(defaultTest=__file__, env={'NOSE_NOCAPTURE' : 1})
    if not ret: sys.exit(1)
