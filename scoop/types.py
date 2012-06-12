#
#    This file is part of Scalable COncurrent Operations in Python (SCOOP).
#
#    SCOOP is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as
#    published by the Free Software Foundation, either version 3 of
#    the License, or (at your option) any later version.
#
#    SCOOP is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with SCOOP. If not, see <http://www.gnu.org/licenses/>.
#
from __future__ import print_function
from collections import namedtuple, deque
import time
import greenlet
import scoop
from scoop.comm import ZMQCommunicator, Shutdown


# This class encapsulates a stopwatch that returns elapse time in seconds. 
class StopWatch(object):
    # initialize stopwatch.
    def __init__(self):
        self.totalTime = 0
        self.startTime = time.time()
        self.halted = False
    # return elapse time.
    def get(self):
        if self.halted:
            return self.totalTime
        else:
            return self.totalTime + time.time()-self.startTime
    # halt stopWatch.
    def halt(self):
        self.halted = True
        self.totalTime += time.time()-self.startTime
    # resume stopwatch.
    def resume(self):
        self.halted = False
        self.startTime = time.time()
    # set stopwatch to zero.
    def reset(self):
        self.__init__()

class CancelledError(Exception):
    """The Future was cancelled."""
    pass
    
class TimeoutError(Exception):
    """The operation exceeded the given deadline."""
    pass

FutureId = namedtuple('FutureId', ['worker', 'rank'])
class Future(object):
    """This class encapsulates and independent future that can be executed in parallel.
    A future can spawn other parallel futures which themselves can recursively spawn
    other futures."""
    
    def __init__(self, parentId, callable, *args, **kargs):
        """Initialize a new future."""
        self.id = FutureId(scoop.control.worker, scoop.control.rank); scoop.control.rank += 1
        self.parentId = parentId          # id of parent
        self.index = None                 # parent index for result
        self.callable = callable          # callable object
        self.args = args                  # arguments of callable
        self.kargs = kargs                # key arguments of callable
        self.creationTime = time.ctime()  # future creation time
        self.stopWatch = StopWatch()      # stop watch for measuring time
        self.greenlet = None              # cooperative thread for running future 
        self.resultValue = None          # future result
        self.exceptionValue = None             # exception raised by callable
        self.callback = []                # set callback
        # insert future into global dictionary
        scoop.control.futureDict[self.id] = self

    def __lt__(self, other):
        """Order futures by creation time."""
        return self.creationTime < other.creationTime
    
    def __str__(self):
        """Convert future to string."""
        return "{0}:{1}{2}={3}".format(self.id,
                                       self.callable.__name__,
                                       self.args,
                                       self.resultValue)
    
    def _switch(self, future):
        """Switch greenlet."""
        scoop.control.current = self
        assert self.greenlet != None, "No greenlet to switch to:\n%s" % self.__dict__
        return self.greenlet.switch(future)

    def cancel(self):
        """Attempt to cancel the call.
        
        :returns: If the call is currently being executed then it cannot
            be cancelled and the method will return False, otherwise
            the call will be cancelled and the method will return True."""
        if self in scoop.control.execQueue.movable:
            self.exceptionValue = CancelledError()
            scoop.control.futureDict.pop(self.id)
            scoop.control.execQueue.remove(self)
            return True
        return False

    def cancelled(self):
        """Returns a status flag of the process.
        
        :returns: True if the call was successfully cancelled, else
            otherwise."""
        return isinstance(self.exceptionValue, CancelledError)

    def running(self):
        """Returns a status flag of the process.
        
        :returns: True if the call is currently being executed and cannot be
            cancelled."""
        return not self.done() and self not in scoop.control.execQueue
        
    def done(self):
        """Returns a status flag of the process.
        
        :returns: True if the call was successfully cancelled or finished
            running."""
        return self.resultValue != None or self.exceptionValue != None

    def result(self, timeout=None):
        """Return the value returned by the call. If the call hasn't yet
        completed then this method will wait up to ''timeout'' seconds. If the
        call hasn't completed in timeout seconds then a TimeoutError will be
        raised. If timeout is not specified or None then there is no limit to
        the wait time.
        
        If the future is cancelled before completing then CancelledError will
        be raised.

        If the call raised an exception then this method will raise the same
        exception.
        
        :returns: The value returned by the call."""
        if not self.done():
            return scoop.futures._join(self)
        if self.exceptionValue != None:
            raise self.exceptionValue
        return self.resultValue

    def exception(self, timeout=None):
        """Return the exception raised by the call. If the call hasn't yet
        completed then this method will wait up to timeout seconds. If the call
        hasn't completed in timeout seconds then a TimeoutError will be raised.
        If timeout is not specified or None then there is no limit to the wait
        time.

        If the future is cancelled before completing then CancelledError will be
        raised.

        If the call completed without raising then None is returned.
        
        :returns: The exception raised by the call."""
        return self.exceptionValue

    def add_done_callback(self, callable):
        """Attaches a callable to the future that will be called when the future
        is cancelled or finishes running. Callable will be called with the
        future as its only argument.

        Added callables are called in the order that they were added and are
        always called in a thread belonging to the process that added them. If
        the callable raises an Exception then it will be logged and ignored. If
        the callable raises another BaseException then behavior is not defined.

        If the future has already completed or been cancelled then callable will
        be called immediately."""
        self.callback.append(callable)


class FutureQueue(object):
    """This class encapsulates a queue of futures that are pending execution.
    Within this class lies the entry points for future communications."""
    def __init__(self):
        """initialize queue to empty elements and create a communication
        object."""
        self.movable = deque()
        self.ready = deque()
        self.inprogress = deque()
        self.socket = ZMQCommunicator()
        self.lowwatermark  = 5
        self.highwatermark = 20
        
    def __iter__(self):
        """iterates over the selectable (cancellable) elements of the queue."""
        for it in (self.movable, self.ready):
            for element in it:
                yield element

    def __len__(self):
        """returns the length of the queue, meaning the sum of it's elements
        lengths."""
        return len(self.movable) + len(self.ready)
    
    def append(self, future):
        """append a future to the queue."""
        if future.resultValue != None and future.index == None:
            self.inprogress.append(future)
        elif future.resultValue != None and future.index != None:
            self.ready.append(future)
        elif future.greenlet != None:
            self.inprogress.append(future)
        else:
            self.movable.append(future)
        # Send oldest futures to the broker [Put that elsewhere?]
        # TODO: Don't send cancelled futures
        while len(self.movable) > self.highwatermark:
            self.socket.sendFuture(self.movable.popleft())
        
    def pop(self):
        """pop the next future from the queue; 
        in progress futures have priority over those that have not yet started;
        higher level futures have priority over lower level ones; """
        self.updateQueue()
        if len(self) < self.lowwatermark:
            self.requestFuture()
        if len(self.ready) != 0:
            return self.ready.pop()
        elif len(self.movable) != 0:
            return self.movable.pop()
        else:
            while len(self) == 0:
                # Block until message arrives
                self.socket._poll(-1)
                self.updateQueue()
            if len(self.ready) != 0:
                return self.ready.pop()
            elif len(self.movable) != 0:
                return self.movable.pop()

    def requestFuture(self):
        for a in range(len(self), self.lowwatermark + 1):
            self.socket.sendRequest()
    
    def updateQueue(self):
        """updates the local queue with elements from the broker."""
        to_remove = []
        for future in self.inprogress:
            if future.index != None:
                self.ready.append(future)
                to_remove.append(future)
        for future in to_remove:
            self.inprogress.remove(future)        
        for future in self.socket.recvFuture():
            if future.id in scoop.control.futureDict:
                scoop.control.futureDict[future.id].resultValue = future.resultValue
                for callback in scoop.control.futureDict[future.id].callback:
                    try:
                        callback.future(scoop.control.futureDict[future.id])
                    except:
                        pass
            else:
                scoop.control.futureDict[future.id] = future
            future = scoop.control.futureDict[future.id]
            self.append(future)

    def remove(self, future):
        """Remove a future from the queue. The future must be cancellable or
        this method will raised a ValueError."""
        self.movable.remove(future)
    
    def select(self, duration):
        """return a list of movable futures that have an estimated total runtime
        of at most "duration" seconds."""
        pass

    def sendResult(self, future):
        future.greenlet = None  # greenlets cannot be pickled
        assert future.done(), "The results are not valid"
        self.socket.sendResult(future)
