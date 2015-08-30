"""
Keyboard controller - provides events for pressed and released keys.
"""

import enum
import pytty
import sys
import types

from . import IRQProvider, IOProvider, Device, IRQList
from ..errors import InvalidResourceError
from ..mm import UINT16_FMT

DEFAULT_PORT_RANGE = 0x100


class ControlMessages(enum.IntEnum):
  HALT = 1025

  CONTROL_MESSAGE_FIRST = 1024


class KeyboardController(IRQProvider, IOProvider, Device):
  def __init__(self, machine, name, streams = None, port = None, irq = None, *args, **kwargs):
    super(KeyboardController, self).__init__(machine, 'input', name, *args, **kwargs)

    self.streams = streams[:] if streams else []
    self.port = port or DEFAULT_PORT_RANGE
    self.ports = range(port, port + 0x0001)
    self.irq = irq or IRQList.CONIO

    self.input = None
    self.input_fd = None

    self.queue = []

  @classmethod
  def create_from_config(cls, machine, config, section):
    return KeyboardController(machine,
                              section,
                              streams = None,
                              port = config.getint(section, 'port', DEFAULT_PORT_RANGE),
                              irq = config.getint(section, 'irq', IRQList.CONIO))

  def __repr__(self):
    return 'basic keyboard controller on [%s] as %s' % (', '.join([UINT16_FMT(port) for port in self.ports]), self.name)

  def enqueue_input(self, stream):
    self.machine.DEBUG('KeyboardController.enqueue_input: stream=%s', stream)

    self.streams.append(stream)

  def close_input(self):
    self.machine.DEBUG('KeyboardController.close_input: input=%s, input_fd=%s', self.input, self.input_fd)

    if self.input_fd is not None:
      self.machine.reactor.remove_fd(self.input_fd)

    if self.input:
      if self.input != sys.stdin:
        self.input.close()

      self.input = None
      self.input_fd = None

  def open_input(self):
    self.machine.DEBUG('KeyboardController.open_input')

    self.close_input()

    if not self.streams:
      self.machine.DEBUG('no additional input streams')
      if not self.queue or self.queue[-1] != ControlMessages.HALT:
        self.machine.DEBUG('signal halt')
        self.queue.append(ControlMessages.HALT)
      return

    stream = self.streams.pop(0)

    if type(stream) == pytty.TTY:
      self.machine.DEBUG('  tty console')

      self.input = stream
      self.input_fd = stream.fileno()

    elif isinstance(stream, types.StringType):
      self.machine.DEBUG('  input file attached')

      self.input = open(stream, 'rb')
      self.input_fd = self.input.fileno()

    elif isinstance(stream, file) or isinstance(stream, orig_file):  # noqa
      self.machine.DEBUG('  opened file')

      self.input = stream
      self.input_fd = self.input.fileno()

    else:
      self.machine.WARN('Unknown input stream type: stream=%s, class=%s', stream, type(stream))
      self.open_input()

    self.machine.DEBUG('input=%s, input_fd=%s', self.input, self.input_fd)

    self.machine.reactor.add_fd(self.input_fd, on_read = self.handle_raw_input, on_error = self.handle_input_error)

  def boot(self):
    self.machine.DEBUG('KeyboardController.boot')

    for port in self.ports:
      self.machine.register_port(port, self)

    self.open_input()

    self.machine.INFO('hid: %s', self)

  def halt(self):
    self.machine.DEBUG('KeyboardController.halt')

    for port in self.ports:
      self.machine.unregister_port(port)

    self.close_input()

  def handle_input_error(self):
    self.machine.DEBUG('KeyboardController.handle_input_error')

    self.open_input()

  def handle_raw_input(self):
    self.machine.DEBUG('KeyboardController.handle_raw_input')

    assert self.input is not False

    try:
      s = self.input.read()

    except IOError, e:
      e.exc_stack = sys.exc_info()
      self.machine.ERROR('failed to read from input: input=%s', self.input)
      self.machine.EXCEPTION(e)
      return

    if isinstance(s, types.StringType) and len(s) == 0:
      # EOF
      self.open_input()
      return

    self.machine.DEBUG('adding %i chars', len(s))

    for c in s:
      self.queue.append(c)

    self.machine.DEBUG('queue now has %i chars', len(self.queue))

    self.machine.trigger_irq(self)

  def __escape_char(self, c):
    return chr(c).replace(chr(10), '\\n').replace(chr(13), '\\r')

  def __read_char(self):
    self.machine.DEBUG('KeyboardController.__read_char')

    while True:
      try:
        c = self.queue.pop(0)

      except IndexError:
        self.machine.DEBUG('no available chars in queue')
        return None

      self.machine.DEBUG('queue now has %i chars', len(self.queue))

      if c == ControlMessages.HALT:
        self.machine.DEBUG('planned halt, execute')
        self.machine.halt()

        return None

      break

    c = ord(c)

    self.machine.DEBUG('c=%s (%s)', c, self.__escape_char(c))

    return c

  def read_u8(self, port):
    self.machine.DEBUG('KeyboardController.read_u8: port=%s', UINT16_FMT(port))

    if port not in self.ports:
      raise InvalidResourceError('Unhandled port: %s', UINT16_FMT(port))

    c = self.__read_char()
    if not c:
      self.machine.DEBUG('empty input, signal it downstream')
      return 0xFF

    self.machine.DEBUG('input byte is %i', c)

    return c