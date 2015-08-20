import functools
import os

from . import mm
from . import snapshot
from . import util

from .interfaces import IMachineWorker, ISnapshotable, IReactorTask

from .console import ConsoleMaster
from .errors import InvalidResourceError
from .log import create_logger
from .util import str2int, LRUCache
from .mm import addr_to_segment, ADDR_FMT, segment_addr_to_addr, UInt16
from .reactor import Reactor
from .snapshot import SnapshotNode

class MachineState(SnapshotNode):
  def __init__(self):
    super(MachineState, self).__init__('nr_cpus', 'nr_cores')

  def get_binary_states(self):
    return [__state for __name, __state in self.get_children().iteritems() if __name.startswith('binary_')]

  def get_core_states(self):
    return [__state for __name, __state in self.get_children().iteritems() if __name.startswith('core')]

class SymbolCache(LRUCache):
  def __init__(self, machine, size, *args, **kwargs):
    super(SymbolCache, self).__init__(machine.LOGGER, size, *args, **kwargs)

    self.machine = machine

  def get_object(self, address):
    cs = addr_to_segment(address)
    address = address & 0xFFFF

    self.machine.DEBUG('SymbolCache.get_object: cs=%s, address=%s', cs, address)

    for binary in self.machine.binaries:
      if binary.cs != cs:
        continue

      return binary.symbol_table[address]

    return (None, None)

class AddressCache(LRUCache):
  def __init__(self, machine, size, *args, **kwargs):
    super(AddressCache, self).__init__(machine.LOGGER, size, *args, **kwargs)

    self.machine = machine

  def get_object(self, symbol):
    self.machine.DEBUG('AddressCache.get_object: symbol=%s', symbol)

    for csr, dsr, sp, ip, symbols in self.machine.binaries:
      if symbol not in symbols:
        continue

      return (csr, symbols[symbol])

    else:
      return None

class BinaryState(SnapshotNode):
  def __init__(self):
    super(BinaryState, self).__init__('path', 'cs', 'ds')

class Binary(ISnapshotable, object):
  binary_id = 0

  def __init__(self, path, run = True):
    super(Binary, self).__init__()

    self.id = Binary.binary_id
    Binary.binary_id += 1

    self.run = run

    self.path = path
    self.cs = None
    self.ds = None
    self.ip = None
    self.symbols = None
    self.regions = None

    self.raw_binary = None

  def load_symbols(self):
    self.raw_binary.load_symbols()
    self.symbol_table = util.SymbolTable(self.raw_binary)

  def save_state(self, parent):
    state = parent.add_child('binary_{}'.format(self.id), BinaryState())

    state.path = self.path
    state.cs = self.cs
    state.ds = self.ds

    map(lambda region: region.save_state(state), self.regions)

  def load_state(self, state):
    pass

  def get_init_state(self):
    return (self.cs, self.ds, self.sp, self.ip, False)

class IRQRouterTask(IReactorTask):
  def __init__(self, machine):
    self.machine = machine

    self.queue = []

  def runnable(self):
    return True

  def run(self):
    while self.queue:
      self.machine.cpus[0].cores[0].irq(self.queue.pop(0).irq)

class CheckLivingCoresTask(IReactorTask):
  def __init__(self, machine):
    self.machine = machine

  def runnable(self):
    return len(self.machine.living_cores()) == 0

  def run(self):
    self.machine.halt()

class Machine(ISnapshotable, IMachineWorker):
  def core(self, cid):
    for _cpu in self.cpus:
      for _core in _cpu.cores:
        if '#%i:#%i' % (_cpu.id, _core.id) == cid:
          return _core
    else:
      self.ERROR('Unknown core: cid=%s', cid)
      return None

  def __init__(self, log_handler = None):
    self.reactor = Reactor()

    # Setup logging
    self.LOGGER = create_logger(handler = log_handler)
    self.DEBUG = self.LOGGER.debug
    self.INFO = self.LOGGER.info
    self.WARN = self.LOGGER.warning
    self.ERROR = self.LOGGER.error
    self.EXCEPTION = self.LOGGER.exception

    self.console = ConsoleMaster(self)
    self.console.register_command('halt', cmd_halt)
    self.console.register_command('boot', cmd_boot)
    self.console.register_command('run', cmd_run)
    self.console.register_command('snap', cmd_snapshot)

    self.irq_router_task = IRQRouterTask(self)
    self.reactor.add_task(self.irq_router_task)

    self.check_living_cores_task = CheckLivingCoresTask(self)
    self.reactor.add_task(self.check_living_cores_task)

    self.symbol_cache = SymbolCache(self, 256)
    self.address_cache = AddressCache(self, 256)

    self.binaries = []

    self.cpus = []
    self.memory = None

    from .io_handlers import IOPortSet
    self.ports = IOPortSet()

    from .irq import IRQSourceSet
    self.irq_sources = IRQSourceSet()

    self.virtual_interrupts = {}
    self.storages = {}

    self.last_state = None
    self.snapshot_file = None

  def cores(self):
    __cores = []
    map(lambda __cpu: __cores.extend(__cpu.cores), self.cpus)
    return __cores

  def living_cores(self):
    __cores = []
    map(lambda __cpu: __cores.extend(__cpu.living_cores()), self.cpus)
    return __cores

  def get_storage_by_id(self, id):
    self.DEBUG('get_storage_by_id: id=%s', id)
    self.DEBUG('storages: %s', str(self.storages))

    return self.storages.get(id, None)

  def get_addr_by_symbol(self, symbol):
    return self.address_cache[symbol]

  def get_symbol_by_addr(self, cs, address):
    return self.symbol_cache[segment_addr_to_addr(cs, address)]

  def save_state(self, parent):
    state = parent.add_child('machine', MachineState())

    state.nr_cpus = self.nr_cpus
    state.nr_cores = self.nr_cores

    map(lambda binary: binary.save_state(state), self.binaries)
    map(lambda __core: __core.save_state(state), self.cores())
    self.memory.save_state(state)

  def load_state(self, state):
    self.nr_cpus = state.nr_cpus
    self.nr_cores = state.nr_cores

    # ignore binary states

    for __cpu in self.cpus:
      cpu_state = state.get_children().get('cpu{}'.format(__cpu.id), None)
      if cpu_state is None:
        self.WARN('State of CPU #%i not found!', __cpu.id)
        continue

      __cpu.load_state(cpu_state)

    self.memory.load_state(state.get_children()['memory'])

  def hw_setup(self, machine_config, machine_in = None, machine_out = None, snapshot_file = None):
    def __print_regions(regions):
      table = [
        ['Section', 'Address', 'Size', 'Flags', 'First page', 'Last page']
      ]

      for r in regions:
        table.append([r.name, ADDR_FMT(r.address), r.size, r.flags, r.pages_start, r.pages_start + r.pages_cnt - 1])

      self.LOGGER.table(table)

    self.config = machine_config

    self.snapshot_file = snapshot_file

    self.nr_cpus = self.config.getint('machine', 'cpus')
    self.nr_cores = self.config.getint('machine', 'cores')

    self.memory = mm.MemoryController(self)

    from .cpu import CPUCacheController
    self.cpu_cache_controller = CPUCacheController(self)

    from .cpu import CPU
    for cpuid in range(0, self.nr_cpus):
      self.cpus.append(CPU(self, cpuid, self.memory, self.cpu_cache_controller, cores = self.nr_cores))

    from .irq import IRQList

    # timer
    # from .irq.timer import TimerIRQ
    # self.register_irq_source(IRQList.TIMER, TimerIRQ(self))

    # console
    from .io_handlers.conio import ConsoleIOHandler
    from .irq.conio import ConsoleIRQ
    self.conio = ConsoleIOHandler(machine_in, machine_out, self)
    self.conio.echo = True

    self.register_port(0x100, self.conio)
    self.register_port(0x101, self.conio)

    self.register_irq_source(IRQList.CONIO, ConsoleIRQ(self, self.conio))

    self.memory.boot()

    from .irq import VIRTUAL_INTERRUPTS
    for index, cls in VIRTUAL_INTERRUPTS.iteritems():
      self.virtual_interrupts[index] = cls(self)

    if self.config.has_option('machine', 'interrupt-routines'):
      binary = Binary(self.config.get('machine', 'interrupt-routines'), run = False)
      self.binaries.append(binary)

      self.INFO('Loading IRQ routines from file %s', binary.path)

      binary.cs, binary.ds, binary.sp, binary.ip, binary.symbols, binary.regions, binary.raw_binary = self.memory.load_file(binary.path)
      binary.load_symbols()

      from .cpu import InterruptVector
      desc = InterruptVector()
      desc.cs = binary.cs
      desc.ds = binary.ds

      def __save_iv(name, table, index):
        if name not in binary.symbols:
          self.DEBUG('Interrupt routine %s not found', name)
          return

        desc.ip = binary.symbols[name].u16
        self.memory.save_interrupt_vector(table, index, desc)

      for i in range(0, IRQList.IRQ_COUNT):
        __save_iv('irq_routine_{}'.format(i), self.memory.irq_table_address, i)

      from .irq import InterruptList
      for i in range(0, InterruptList.INT_COUNT):
        __save_iv('int_routine_{}'.format(i), self.memory.int_table_address, i)

      __print_regions(binary.regions)

      self.INFO('')

    for binary_section in self.config.iter_binaries():
      binary = Binary(self.config.get(binary_section, 'file'))
      self.binaries.append(binary)

      self.INFO('Loading binary from file %s', binary.path)

      binary.cs, binary.ds, binary.sp, binary.ip, binary.symbols, binary.regions, binary.raw_binary = self.memory.load_file(binary.path)
      binary.load_symbols()

      entry_label = self.config.get(binary_section, 'entry', 'main')
      entry_addr = binary.symbols.get(entry_label, None)

      if entry_addr is None:
        self.WARN('Entry point "%s" of binary %s not found', entry_label, binary.path)
        entry_addr = UInt16(0)

      binary.ip = entry_addr.u16

      __print_regions(binary.regions)

      self.INFO('')

    for mmap_section in self.config.iter_mmaps():
      _get     = functools.partial(self.config.get, mmap_section)
      _getbool  = functools.partial(self.config.getbool, mmap_section)
      _getint  = functools.partial(self.config.getint, mmap_section)

      self.memory.mmap_area(_get('file'),
                            _getint('address'),
                            _getint('size'),
                            offset = _getint('offset', 0),
                            access = _get('access', 'r'),
                            shared = _getbool('shared', False))

    # Breakpoints
    from .debugging import add_breakpoint

    for bp_section in self.config.iter_breakpoints():
      _get     = functools.partial(self.config.get, bp_section)
      _getbool = functools.partial(self.config.getbool, bp_section)
      _getint  = functools.partial(self.config.getint, bp_section)

      core = self.core(_get('core', '#0:#0'))

      address = _get('address', '0x000000')
      if address[0].isdigit():
        address = UInt16(str2int(address))

      else:
        for binary in self.binaries:
          symbol_address = binary.symbols.get(address, None)
          if symbol_address is not None:
            address = symbol_address
            break

      if address is None:
        self.ERROR('Unknown breakpoint address: %s on %s', _get('address', '0x000000'), _get('core', '#0:#0'))
        continue

      add_breakpoint(core, address.u16, ephemeral = _getbool('ephemeral', False), countdown = _getint('countdown', 0))

    # Storage
    from .blockio import STORAGES

    for st_section in self.config.iter_storages():
      _get     = functools.partial(self.config.get, st_section)
      _getbool = functools.partial(self.config.getbool, st_section)
      _getint  = functools.partial(self.config.getint, st_section)

      self.storages[_getint('id')] = STORAGES[_get('driver')](self, _getint('id'), _get('file'))

  @property
  def exit_code(self):
    self.__exit_code = 0

    for __cpu in self.cpus:
      for __core in __cpu.cores:
        if __core.exit_code != 0:
          self.__exit_code = __core.exit_code

    return self.__exit_code

  def register_irq_source(self, index, src, reassign = False):
    if self.irq_sources[index]:
      if not reassign:
        raise InvalidResourceError('IRQ already assigned: {}'.format(index))

      for i in range(0, len(self.irq_sources)):
        if not self.irq_sources[i]:
          index = i
          break
      else:
        raise InvalidResourceError('IRQ already assigned, no available free IRQ: {}'.format(index))

    self.irq_sources[index] = src
    src.irq = index
    return index

  def unregister_irq_source(self, index):
    self.irq_sources[index] = None

  def register_port(self, port, handler):
    if port in self.ports:
      raise IndexError('Port already assigned: {}'.format(port))

    self.ports[port] = handler

  def unregister_port(self, port):
    del self.ports[port]

  def trigger_irq(self, handler):
    self.irq_router_task.queue.append(handler)

  def boot(self):
    self.DEBUG('Machine.boot')

    self.console.boot()

    map(lambda __port: __port.boot(), self.ports)
    map(lambda __irq: __irq.boot(), [irq_source for irq_source in self.irq_sources if irq_source is not None])
    map(lambda __storage: __storage.boot(), self.storages.itervalues())

    init_states = [binary.get_init_state() for binary in self.binaries if binary.run]
    map(lambda __cpu: __cpu.boot(init_states), self.cpus)

    self.INFO('Guest terminal available at %s', self.conio.get_terminal_dev())

  def run(self):
    self.DEBUG('Machine.run')

    map(lambda __port: __port.run(), self.ports)
    map(lambda __irq: __irq.run(), [irq_source for irq_source in self.irq_sources if irq_source is not None])
    map(lambda __storage: __storage.run(), self.storages.itervalues())

    map(lambda __cpu: __cpu.run(), self.cpus)

    self.reactor.run()

  def suspend(self):
    self.DEBUG('Machine.suspend')

    map(lambda __cpu: __cpu.suspend(), self.cpus)

  def wake_up(self):
    self.DEBUG('Machine.wake_up')

    map(lambda __cpu: __cpu.wake_up(), self.cpus)

  def die(self, exc):
    self.DEBUG('Machine.die: exc=%s', exc)

    self.EXCEPTION(exc)

    self.halt()

  def halt(self):
    self.DEBUG('Machine.halt')

    if self.snapshot_file is not None:
      self.snapshot(self.snapshot_file)

    else:
      self.capture_state()

    map(lambda __irq: __irq.halt(),         [irq_source for irq_source in self.irq_sources if irq_source is not None])
    map(lambda __port: __port.halt(),       self.ports)
    map(lambda __storage: __storage.halt(), self.storages.itervalues())
    map(lambda __cpu: __cpu.halt(),         self.cpus)

    self.memory.halt()

    self.console.halt()

    self.reactor.remove_task(self.irq_router_task)
    self.reactor.remove_task(self.check_living_cores_task)

  def capture_state(self, suspend = False):
    self.last_state = snapshot.VMState.capture_vm_state(self, suspend = suspend)

  def snapshot(self, path):
    self.capture_state()
    self.last_state.save(path)
    self.INFO('VM snapshot save in %s', path)

def cmd_boot(console, cmd):
  """
  Setup HW, load binaries, init everything
  """

  M = console.master.machine

  M.boot()
  M.console.unregister_command('boot')

def cmd_run(console, cmd):
  """
  Start execution of loaded binaries
  """

  M = console.master.machine

  M.run()
  M.console.unregister_command('run')

def cmd_halt(console, cmd):
  """
  Halt execution
  """

  M = console.master.machine

  M.halt()

  M.INFO('VM halted by user')

def cmd_snapshot(console, cmd):
  """
  Create snapshot
  """

  M = console.master.machine

  state = snapshot.VMState.capture_vm_state(M)

  filename = 'ducky-core.{}'.format(os.getpid())
  state.save(filename)

  M.INFO('Snapshot saved as %s', filename)
  console.writeln('Snapshot saved as %s', filename)
