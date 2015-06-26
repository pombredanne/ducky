import array
import mmap

from ctypes import LittleEndianStructure, c_ubyte, c_ushort, c_uint, sizeof

from ..errors import AccessViolationError, InvalidResourceError
from ..util import debug, align
from ..snapshot import SnapshotNode, ISnapshotable

# Types
from ctypes import c_byte as i8  # NOQA
from ctypes import c_short as i16  # NOQA
from ctypes import c_int as i32  # NOQA

from ctypes import c_ubyte as u8  # NOQA
from ctypes import c_ushort as u16  # NOQA
from ctypes import c_uint as u32  # NOQA

MEM_IRQ_TABLE_ADDRESS   = 0x000000
MEM_INT_TABLE_ADDRESS   = 0x000100

PAGE_SHIFT = 8
#: Size of memory page, in bytes.
PAGE_SIZE = (1 << PAGE_SHIFT)
PAGE_MASK = (~(PAGE_SIZE - 1))

SEGMENT_SHIFT = 16
#: Size of segment, in pages
SEGMENT_SIZE  = 256  # pages
SEGMENT_PROTECTED = 0  # first segment is already allocated

def __var_to_int(v):
  if type(v) == UInt8:
    return v.u8

  if type(v) == UInt16:
    return v.u16

  if type(v) == UInt24:
    return v.u24

  if type(v) == UInt32:
    return v.u32

  return v

def UINT8_FMT(v):
  return '0x{:02X}'.format(__var_to_int(v) & 0xFF)

def UINT16_FMT(v):
  return '0x{:04X}'.format(__var_to_int(v) & 0xFFFF)

def UINT24_FMT(v):
  return '0x{:06X}'.format(__var_to_int(v) & 0xFFFFFF)

def UINT32_FMT(v):
  return '0x{:08X}'.format(__var_to_int(v))

def SEGM_FMT(segment):
  return UINT8_FMT(segment)

def ADDR_FMT(address):
  return UINT24_FMT(address)

def SIZE_FMT(size):
  return str(size)

def OFFSET_FMT(offset):
  s = '-' if offset < 0 else ''

  return '{}0x{:04X}'.format(s, abs(offset))

class MalformedBinaryError(Exception):
  pass

class UInt8(LittleEndianStructure):
  _pack_ = 0
  _fields_ = [
    ('u8', c_ubyte)
  ]

  def __repr__(self):
    return '<(u8) 0x{:02X}>'.format(self.u8)

class UInt16(LittleEndianStructure):
  _pack_ = 0
  _fields_ = [
    ('u16', c_ushort)
  ]

  def __repr__(self):
    return '<(u16) 0x{:04X}>'.format(self.u16)

# Yes, this one is larger but it's used only for transporting
# addresses between CPUs and memory controller => segment
# register and u16 have to fit in.
class UInt24(LittleEndianStructure):
  _pack_ = 0
  _fields_ = [
    ('u24', c_uint, 24)
  ]

  def __repr__(self):
    return '<(u24) 0x{:06X}>'.format(self.u24)

class UInt32(LittleEndianStructure):
  _pack_ = 0
  _fields_ = [
    ('u32', c_uint)
  ]

  def __repr__(self):
    return '<(u32) 0x{:06X}>'.format(self.u32)

def segment_base_addr(segment):
  return segment * SEGMENT_SIZE * PAGE_SIZE

def segment_addr_to_addr(segment, addr):
  return segment * SEGMENT_SIZE * PAGE_SIZE + addr

def addr_to_segment(addr):
  return (addr & 0xFF0000) >> 16

def addr_to_page(addr):
  return (addr & PAGE_MASK) >> PAGE_SHIFT

def addr_to_offset(addr):
  return (addr & (PAGE_SIZE - 1))

def area_to_pages(addr, size):
  return ((addr & PAGE_MASK) >> PAGE_SHIFT, align(PAGE_SIZE, size) / PAGE_SIZE)

class MemoryPageState(SnapshotNode):
  def __init__(self, *args, **kwargs):
    super(MemoryPageState, self).__init__('index', 'read', 'write', 'execute', 'dirty', 'stack', 'content')

class MemoryPage(object):
  """
  Base class for all memory pages of any kinds.

  :param ducky.mm.MemoryController controller: Controller that owns this page.
  :param int index: Serial number of this page.
  """

  def __init__(self, controller, index):
    super(MemoryPage, self).__init__()

    self.controller = controller
    self.index = index

    self.base_address = self.index * PAGE_SIZE
    self.segment_address = self.base_address % (SEGMENT_SIZE * PAGE_SIZE)

    self.read    = False
    self.write   = False
    self.execute = False
    self.dirty   = False
    self.stack   = False

  def save_state(self, parent):
    """
    Create state of this page, and attach it to snapshot tree.

    :param parent: Parent snapshot node.
    :type parent: ducky.snapshot.SnapshotNode
    """

    state = parent.add_child('page_{}'.format(self.index), MemoryPageState())

    state.index = self.index

    state.content = [int(i) for i in self.data]
    state.read = self.read
    state.write = self.write
    state.execute = self.execute
    state.dirty = self.dirty
    state.stack = self.stack

  def load_state(self, state):
    """
    Restore page from a snapshot.
    """

    for i in range(0, PAGE_SIZE):
      self.data[i] = state.content[i]

    self.read = state.read
    self.write = state.write
    self.execute = state.execute
    self.dirty = state.dirty
    self.stack = state.stack

  def flags_reset(self):
    """
    Reset all page flags to ``False``.
    """

    self.read = False
    self.write = False
    self.execute = False
    self.dirty = False

  def flags_str(self):
    """
    Return string representing page flags. Each position in string represents
    one of the flags. If flag is ``False``, ``-`` is emmited, otherwise a letter
    symbolizing that flag stands on the position.

    E.g. ``RW-D`` are flags of page that is readable, writable and dirty but
    not executable.

    :rtype: string
    """

    return ''.join([
      'R' if self.read else '-',
      'W' if self.write else '-',
      'X' if self.execute else '-',
      'D' if self.dirty else '-'
    ])

  def check_access(self, offset, access):
    """
    Return ``True`` if required access is valid for this page.

    :param int offset: Offset of the byte caller wants to manipulate. This
      parameter is only informative, used for debugging purposes.
    :param string access: One of ``read``, ``write`` or ``execute``.
    :rtype: bool
    :raises ducky.errors.AccessViolationError: when access is denied.
    """

    debug('mp.check_access: page=%s, offset=%s, access=%s, %s', self.index, offset, access, self.flags_str())

    if access == 'read' and not self.read:
      raise AccessViolationError('Not allowed to read from memory: page=%s, offset={}'.format(self.index, offset))

    if access == 'write' and not self.write:
      raise AccessViolationError('Not allowed to write to memory: page=%s, offset={}'.format(self.index, offset))

    if access == 'execute' and not self.execute:
      raise AccessViolationError('Not allowed to execute from memory: page=%s, offset={}'.format(self.index, offset))

    return True

  def __len__(self):
    """
    :return: length of this page. By default, all pages have the same length.
    :rtype: int
    """

    return PAGE_SIZE

  def do_clear(self):
    """
    Clear page.

    This operation is implemented by child classes.
    """

    raise AccessViolationError('Not allowed to clear memory on this address: page={}'.format(self.index))

  def do_read_u8(self, offset):
    """
    Read byte.

    This operation is implemented by child classes.

    :param int offset: offset of requested byte.
    :rtype: int
    """

    raise AccessViolationError('Not allowed to access memory on this address: page=%s, offset={}'.format(self.index, offset))

  def do_read_u16(self, offset):
    """
    Read word.

    This operation is implemented by child classes.

    :param int offset: offset of requested word.
    :rtype: int
    """

    raise AccessViolationError('Not allowed to access memory on this address: page=%s, offset={}'.format(self.index, offset))

  def do_read_u32(self, offset):
    """
    Read longword.

    This operation is implemented by child classes.

    :param int offset: offset of requested longword.
    :rtype: int
    """

    raise AccessViolationError('Not allowed to access memory on this address: page=%s, offset={}'.format(self.index, offset))

  def do_write_u8(self, offset, value):
    """
    Write byte.

    This operation is implemented by child classes.

    :param int offset: offset of requested byte.
    :param int value: value to write into memory.
    """

    raise AccessViolationError('Not allowed to access memory on this address: page=%s, offset={}'.format(self.index, offset))

  def do_write_u16(self, offset, value):
    """
    Write word.

    This operation is implemented by child classes.

    :param int offset: offset of requested word.
    :param int value: value to write into memory.
    """

    raise AccessViolationError('Not allowed to access memory on this address: page=%s, offset={}'.format(self.index, offset))

  def do_write_u32(self, offset, value):
    """
    Write longword.

    This operation is implemented by child classes.

    :param int offset: offset of requested longword.
    :param int value: value to write into memory.
    """

    raise AccessViolationError('Not allowed to access memory on this address: page=%s, offset={}'.format(self.index, offset))

  def clear(self, privileged = False):
    """
    Clear page. Checks page flags, and calls ``do_clear`` method to perform the
    `clear` operation.

    :param bool privileged: if not ``True``, page access is checked first.
    :raises ducky.errors.AccessViolationError: when page is not writtable.
    """

    debug('mp.clear: page=%s, priv=%s', self.index, privileged)

    privileged or self.check_access(self.base_address, 'write')

    self.do_clear()

  def read_u8(self, offset, privileged = False):
    """
    Read byte. Checks page flags, and calls ``do_read_u8`` method to perform
    the `read` operation.

    :param int offset: offset of requested byte.
    :param bool privileged: if not ``True``, page access is checked first.
    :rtype: int
    :raises ducky.errors.AccessViolationError: when page is not readable.
    """

    debug('mp.read_u8: page=%s, offset=%s, priv=%s', self.index, offset, privileged)

    privileged or self.check_access(offset, 'read')

    return self.do_read_u8(offset)

  def read_u16(self, offset, privileged = False):
    """
    Read word. Checks page flags, and calls ``do_read_u16`` method to perform
    the `read` operation.

    :param int offset: offset of requested word.
    :param bool privileged: if not ``True``, page access is checked first.
    :raises ducky.errors.AccessViolationError: when page is not readable.
    """

    debug('mp.read_u16: page=%s, offset=%s, priv=%s', self.index, offset, privileged)

    privileged or self.check_access(offset, 'read')

    return self.do_read_u16(offset)

  def read_u32(self, offset, privileged = False):
    """
    Read longword. Checks page flags, and calls ``do_read_u32`` method to
    perform the `read` operation.

    :param int offset: offset of requested longword.
    :param bool privileged: if not ``True``, page access is checked first.
    :raises ducky.errors.AccessViolationError: when page is not readable.
    """

    debug('mp.read_u32: page=%s, offset=%s, priv=%s', self.index, offset, privileged)

    privileged or self.check_access(offset, 'read')

    return self.do_read_u32(offset)

  def write_u8(self, offset, value, privileged = False, dirty = True):
    """
    Write byte. Checks page flags, and calls ``do_write_u8`` method to perform
    the `write` operation.

    :param int offset: offset of requested byte.
    :param int value: value to write into memory.
    :param bool privileged: if not ``True``, page access is checked first.
    :param bool dirty: if ``True``, page ``dirty`` flag is set to ``True``.
    :raises ducky.errors.AccessViolationError: when page is not writable.
    """

    debug('mp.write_u8: page=%s, offset=%s, value=%s, priv=%s, dirty=%s', self.index, offset, value, privileged, dirty)

    privileged or self.check_access(offset, 'write')

    self.do_write_u8(offset, value)
    if dirty:
      self.dirty = True

  def write_u16(self, offset, value, privileged = False, dirty = True):
    """
    Write word. Checks page flags, and calls ``do_write_u16`` method to perform
    the `write` operation.

    :param int offset: offset of requested word.
    :param int value: value to write into memory.
    :param bool privileged: if not ``True``, page access is checked first.
    :param bool dirty: if ``True``, page ``dirty`` flag is set to ``True``.
    :raises ducky.errors.AccessViolationError: when page is not writable.
    """

    debug('mp.write_u16: page=%s, offset=%s, value=%s, priv=%s, dirty=%s', self.index, offset, value, privileged, dirty)

    privileged or self.check_access(offset, 'write')

    self.do_write_u16(offset, value)
    if dirty:
      self.dirty = True

  def write_u32(self, offset, value, privileged = False, dirty = True):
    """
    Write longbyte. Checks page flags, and calls ``do_write_u32`` method to
    perform the `write` operation.

    :param int offset: offset of requested longword.
    :param int value: value to write into memory.
    :param bool privileged: if not ``True``, page access is checked first.
    :param bool dirty: if ``True``, page ``dirty`` flag is set to ``True``.
    :raises ducky.errors.AccessViolationError: when page is not writable.
    """

    debug('mp.write_u32: page=%s, offset=%s, value=%s, priv=%s, dirty=%s', self.index, offset, value, privileged, dirty)

    privileged or self.check_access(offset, 'write')

    self.do_write_u32(offset, value)
    if dirty:
      self.dirty = True

class AnonymousMemoryPage(MemoryPage):
  """
  "Anonymous" memory page - this page is just a plain array of bytes, and is
  not backed by any storage. Its content lives only in the memory.

  Page is created with all bytes set to zero.
  """

  def __init__(self, controller, index):
    super(AnonymousMemoryPage, self).__init__(controller, index)

    self.data = array.array('B', [0 for _ in range(0, PAGE_SIZE)])

  def do_clear(self):
    for i in range(0, PAGE_SIZE):
      self.data[i] = 0

  def do_read_u8(self, offset):
    debug('mp.do_read_u8: page=%s, offset=%s', self.index, offset)

    return self.data[offset]

  def do_read_u16(self, offset):
    debug('mp.do_read_u16: page=%s, offset=%s', self.index, offset)

    return self.data[offset] | (self.data[offset + 1] << 8)

  def do_read_u32(self, offset):
    debug('mp.do_read_u32: page=%s, offset=%s', self.index, offset)

    return self.data[offset] | (self.data[offset + 1] << 8) | (self.data[offset + 2] << 16) | (self.data[offset + 3] << 24)

  def do_write_u8(self, offset, value):
    debug('mp.do_write_u8: page=%s, offset=%s, value=%s', self.index, offset, value)

    self.data[offset] = value

  def do_write_u16(self, offset, value):
    debug('mp.do_write_u16: page=%s, offset=%s, value=%s', self.index, offset, value)

    self.data[offset]     =  value & 0x00FF
    self.data[offset + 1] = (value & 0xFF00) >> 8

  def do_write_u32(self, offset, value):
    debug('mp.do_write_u32: page=%s, offset=%s, value=%s', self.index, offset, value)

    self.data[offset]     =  value &       0xFF
    self.data[offset + 1] = (value &     0xFF00) >> 8
    self.data[offset + 2] = (value &   0xFF0000) >> 16
    self.data[offset + 3] = (value & 0xFF000000) >> 24

class MMapMemoryPage(MemoryPage):
  """
  Memory page backed by an external file that is accessible via ``mmap()``
  call. It's a part of one of mm.MMapArea instances, and if such area was
  opened as `shared`, every change in this page content will affect the
  content of external file, and vice versa, every change of external file
  will be reflected in content of this page (if this page lies in affected
  area).
  """

  def __init__(self, controller, index, data, offset):
    super(MMapMemoryPage, self).__init__(controller, index)

    self.data = data
    self.__offset = offset

  def save_state(self, state):
    pass

  def load_state(self, state):
    pass

  def do_clear(self):
    for i in range(0, PAGE_SIZE):
      self.data[i] = 0

  def __get_byte(self, offset):
    return ord(self.data[self.__offset + offset])

  def __put_char(self, offset, b):
    self.data[self.__offset + offset] = chr(b)

  def do_read_u8(self, offset):
    debug('mp.do_read_u8: page=%s, offset=%s', self.index, offset)

    return self.__get_byte(offset)

  def do_read_u16(self, offset):
    debug('mp.do_read_u16: page=%s, offset=%s', self.index, offset)

    return self.__get_byte(offset) | (self.__get_byte(offset + 1) << 8)

  def do_read_u32(self, offset):
    debug('mp.do_read_u32: page=%s, offset=%s', self.index, offset)

    return self.__get_byte(offset) | (self.__get_byte(offset + 1) << 8) | (self.__get_byte(offset + 2) << 16) | (self.__get_byte(offset + 3) << 24)

  def do_write_u8(self, offset, value):
    debug('mp.do_write_u8: page=%s, offset=%s, value=%s', self.index, offset, value)

    self.__put_char(offset, value)

  def do_write_u16(self, offset, value):
    debug('mp.do_write_u16: page=%s, offset=%s, value=%s', self.index, offset, value)

    self.__put_char(offset, value & 0x00FF)
    self.__put_char(offset + 1, (value & 0xFF00) >> 8)

  def do_write_u32(self, offset, value):
    debug('mp.do_write_u32: page=%s, offset=%s, value=%s', self.index, offset, value)

    self.__put_char(offset, value & 0x00FF)
    self.__put_char(offset + 1, (value & 0xFF00) >> 8)
    self.__put_char(offset + 2, (value & 0xFF0000) >> 16)
    self.__put_char(offset + 3, (value & 0xFF000000) >> 24)

class MMapAreaState(SnapshotNode):
  def __init__(self):
    super(MMapAreaState, self).__init__('address', 'size', 'path', 'offset')

class MMapArea(object):
  def __init__(self, address, size, file_path, offset, pages_start, pages_cnt):
    super(MMapArea, self).__init__()

    self.address = address
    self.size = size
    self.file_path = file_path
    self.offset = offset
    self.pages_start = pages_start
    self.pages_cnt = pages_cnt

  def save_state(self, parent):
    pass

  def load_state(self, state):
    pass

class MemoryRegionState(SnapshotNode):
  def __init__(self):
    super(MemoryRegionState, self).__init__('name', 'address', 'size', 'flags', 'pages_start', 'pages_cnt')

class MemoryRegion(ISnapshotable, object):
  region_id = 0

  def __init__(self, name, address, size, flags):
    super(MemoryRegion, self).__init__()

    self.id = MemoryRegion.region_id
    MemoryRegion.region_id += 1

    self.name = name
    self.address = address
    self.size = size
    self.flags = flags

    self.pages_start, self.pages_cnt = area_to_pages(self.address, self.size)

    debug('MemoryRegion: name=%s, address=%s, size=%s, flags=%s, pages_start=%s, pages_cnt=%s', name, address, size, flags, self.pages_start, self.pages_cnt)

  def save_state(self, parent):
    state = parent.add_child('memory_region_{}'.format(self.id), MemoryRegionState())

    state.name = self.name
    state.address = self.address
    state.size = self.size
    state.flags = self.flags.to_uint16()
    state.pages_start = self.pages_start
    state.pages_cnt = self.pages_cnt

  def load_state(self, state):
    pass

class MemoryState(SnapshotNode):
  def __init__(self):
    super(MemoryState, self).__init__('size', 'irq_table_address', 'int_table_address', 'segments')

  def get_page_states(self):
    return [__state for __name, __state in self.get_children().iteritems() if __name.startswith('page_')]

class MemoryController(object):
  """
  Memory controller handles all operations regarding main memory.

  :param ducky.machine.Machine machine: virtual machine that owns this controller.
  :param int size: size of memory, in bytes.
  :raises ducky.errors.InvalidResourceError: when memory size is not multiple of
    :py:data:`ducky.mm.PAGE_SIZE`, or when size is not multiply of
    :py:data:`ducky.mm.SEGMENT_SIZE` pages.
  """

  def __init__(self, machine, size = 0x1000000):
    super(MemoryController, self).__init__()

    if size % PAGE_SIZE != 0:
      raise InvalidResourceError('Memory size must be multiple of PAGE_SIZE')

    if size % (SEGMENT_SIZE * PAGE_SIZE) != 0:
      raise InvalidResourceError('Memory size must be multiple of SEGMENT_SIZE')

    self.machine = machine

    self.force_aligned_access = self.machine.config.getbool('memory', 'force-aligned-access', default = False)

    self.__size = size
    self.__pages_cnt = size / PAGE_SIZE
    self.__pages = {}

    self.__segments_cnt = size / (SEGMENT_SIZE * PAGE_SIZE)
    self.__segments = {}

    # mmap
    self.opened_mmap_files = {}  # path: (cnt, file)
    self.mmap_areas = {}

    self.irq_table_address = MEM_IRQ_TABLE_ADDRESS
    self.int_table_address = MEM_INT_TABLE_ADDRESS

  def save_state(self, parent):
    debug('mc.save_state')

    state = parent.add_child('memory', MemoryState())

    state.size = self.__size
    state.irq_table_address = self.irq_table_address
    state.int_table_address = self.int_table_address

    state.segments = []
    for segment in self.__segments.keys():
      state.segments.append(segment)

    for page in self.__pages.values():
      page.save_state(state)

  def load_state(self, state):
    self.size = state.size
    self.irq_table_address = state.irq_table_address
    self.int_table_address = state.int_table_address

    for segment in state.segments:
      self.__segments[segment] = True

    for page_state in state.get_children():
      page = self.get_page(page_state.index)
      page.load_state(page_state)

  def alloc_segment(self):
    """
    Reserve one of free memory segments.

    :returns: index of reserved segment.
    :rtype: int
    :raises ducky.errors.InvalidResourceError: when there are no free segments.
    """

    debug('mc.alloc_segment')

    for i in range(0, self.__segments_cnt):
      if i in self.__segments:
        continue

      # No SegmentMapEntry flags are in use right now, just keep this option open
      debug('mc.alloc_segment: segment=%s', i)

      self.__segments[i] = True
      return i

    raise InvalidResourceError('No free segment available')

  def get_page(self, index):
    """
    Return memory page, specified by its index from the beginning of memory.

    :param int index: index of requested page.
    :rtype: :py:class:`ducky.mm.MemoryPage`
    :raises ducky.errors.AccessViolationError: when requested page is not allocated.
    """

    if index not in self.__pages:
      raise AccessViolationError('Page {} not allocated yet'.format(index))

    return self.__pages[index]

  def __alloc_page(self, index, area_index):
    """
    Allocate new anonymous page for usage. The first available index is used.

    Be aware that this method does NOT check if page is already allocated. If
    it is, it is just overwritten by new anonymous page.

    :param int index: index of requested page.
    :param int area_index: index of requested page in area, when the whole set
      of pages is being allocated at once.
    :returns: newly reserved page.
    :rtype: :py:class:`ducky.mm.AnonymousMemoryPage`
    """

    self.__pages[index] = AnonymousMemoryPage(self, index)
    return self.__pages[index]

  def alloc_specific_page(self, index):
    """
    Allocate new anonymous page with specific index for usage.

    :param int index: allocate page with this particular index.
    :returns: newly reserved page.
    :rtype: :py:class:`ducky.mm.AnonymousMemoryPage`
    :raises ducky.errors.AccessViolationError: when page is already allocated.
    """

    debug('mc.alloc_specific_page: index=%s', index)

    if index in self.__pages:
      raise AccessViolationError('Page {} is already allocated'.format(index))

    return self.__alloc_page(index, None)

  def alloc_pages(self, segment = None, count = 1):
    """
    Allocate continuous sequence of anonymous pages.

    :param int segment: if not ``None``, allocated pages will be from this
      segment.
    :param int count: number of requested pages.
    :returns: list of newly allocated pages.
    :rtype: ``list`` of :py:class:`ducky.mm.AnonymousMemoryPage`
    :raises ducky.errors.InvalidResourceError: when there is no available sequence of
      pages.
    """

    debug('mc.alloc_pages: segment=%s, count=%s', segment if segment else '', count)

    if segment is not None:
      pages_start = segment * SEGMENT_SIZE
      pages_cnt = SEGMENT_SIZE
    else:
      pages_start = 0
      pages_cnt = self.__pages_cnt

    debug('mc.alloc_pages: page=%s, cnt=%s', pages_start, pages_cnt)

    for i in xrange(pages_start, pages_start + pages_cnt):
      for j in xrange(i, i + count):
        if j in self.__pages:
          break

      else:
        return [self.__alloc_page(j, None) for j in xrange(i, i + count)]

    else:
      raise InvalidResourceError('No sequence of free pages available')

  def alloc_page(self, segment = None):
    """
    Allocate new anonymous page for usage. The first available index is used.

    :param int segment: if not ``None``, allocated page will be from this
      segment.
    :returns: newly reserved page.
    :rtype: :py:class:`ducky.mm.AnonymousMemoryPage`
    :raises ducky.errors.InvalidResourceError: when there is no available page.
    """

    debug('mc.alloc_page: segment=%s', segment if segment else '')

    if segment is not None:
      pages_start = segment * SEGMENT_SIZE
      pages_cnt = SEGMENT_SIZE
    else:
      pages_start = 0
      pages_cnt = self.__pages_cnt

    debug('mc.alloc_page: page=%s, cnt=%s', pages_start, pages_cnt)

    for i in range(pages_start, pages_start + pages_cnt):
      if i not in self.__pages:
        debug('mc.alloc_page: page=%s', i)
        return self.__alloc_page(i, None)

    raise InvalidResourceError('No free page available')

  def alloc_stack(self, segment = None):
    """
    Allocate page for a stack. Such page does not differ from other but this
    pages are requested from different places of virtual machine, therefore
    this shortcut method.

    :param int segment: if not ``None``, , allocated page will be from this
      segment.
    :returns: newly allocated page, and base address of the next memory page.
      This address is in fact a stack pointer for storing the first value on
      newly allocated stack (`decrement and store`).
    :rtype: (:py:class:`ducky.mm.AnonymousMemoryPage`, ``int``)
    :raises ducky.errors.InvalidResourceError: when there is no available page.
    """

    pg = self.alloc_page(segment)
    pg.read = True
    pg.write = True
    pg.stack = True

    return (pg, pg.segment_address + PAGE_SIZE)

  def free_page(self, page):
    """
    Free memory page when it's no longer needed.

    :param ducky.mm.MemoryPage page: page to be freed.
    """

    debug('mc.free_page: page=%i, base=%s, segment=%s', page.index, page.base_address, page.segment_address)

    del self.__pages[page.index]

  def free_pages(self, page, count = 1):
    """
    Free a continuous sequence of pages when they are no longer needed.

    :param ducky.mm.MemoryPage page: first page in series.
    :param int count: number of pages.
    """

    debug('mc.free_pages: page=%i, base=%s, segment=%s, count=%s', page.index, page.base_address, page.segment_address, count)

    for i in range(page.index, page.index + count):
      self.free_page(self.__pages[i])

  def for_each_page(self, pages_start, pages_cnt, fn):
    debug('mc.for_each_page: pages_start=%i, pages_cnt=%i, fn=%s', pages_start, pages_cnt, fn)

    area_index = 0
    for page_index in range(pages_start, pages_start + pages_cnt):
      fn(page_index, area_index)
      area_index += 1

  def for_each_page_in_area(self, address, size, fn):
    pages_start, pages_cnt = area_to_pages(address, size)

    self.for_each_page(pages_start, pages_cnt, fn)

  def boot(self):
    """
    Prepare memory controller for immediate usage by other components.
    """

    # Reserve the first segment for system usage
    self.alloc_segment()

    # IRQ table
    self.__alloc_page(addr_to_page(self.irq_table_address), None).read = True

    # INT table
    self.__alloc_page(addr_to_page(self.int_table_address), None).read = True

  def update_area_flags(self, address, size, flag, value):
    debug('mc.update_area_flags: address=%s, size=%s, flag=%s, value=%i', address, size, flag, value)

    self.for_each_page_in_area(address, size, lambda page_index, area_index: setattr(self.get_page(page_index), flag, value))

  def update_pages_flags(self, pages_start, pages_cnt, flag, value):
    debug('mc.update_pages_flags: page=%s, cnt=%s, flag=%s, value=%i', pages_start, pages_cnt, flag, value)

    self.for_each_page(pages_start, pages_cnt, lambda page_index, area_index: setattr(self.get_page(page_index), flag, value))

  def reset_area_flags(self, address, size):
    debug('mc.reset_area_flags: address=%s, size=%s', address, size)

    self.for_each_page_in_area(address, size, lambda page_index, area_index: self.get_page(page_index).flags_reset())

  def reset_pages_flags(self, pages_start, pages_cnt):
    debug('mc.reset_pages_flags: page=%s, size=%s', pages_start, pages_cnt)

    self.for_each_page(pages_start, pages_cnt, lambda page_index, area_index: self.get_page(page_index).flags_reset())

  def __load_content_u8(self, segment, base, content):
    from ..cpu.assemble import SpaceSlot

    bsp  = segment_addr_to_addr(segment, base)
    sp   = bsp
    size = len(content)

    debug('mc.__load_content_u8: segment=%s, base=%s, size=%s, sp=%s', segment, base, size, sp)

    for i in content:
      if type(i) == SpaceSlot:
        sp += i.size.u16
      else:
        self.write_u8(sp, i.u8, privileged = True)
        sp += 1

  def __load_content_u16(self, segment, base, content):
    bsp  = segment_addr_to_addr(segment, base)
    sp   = bsp
    size = len(content) * 2

    debug('mc.__load_content_u16: segment=%s, base=%s, size=%s, sp=%s', segment, base, size, sp)

    for i in content:
      self.write_u16(sp, i.u16, privileged = True)
      sp += 2

  def __load_content_u32(self, segment, base, content):
    bsp = segment_addr_to_addr(segment, base)
    sp   = bsp
    size = len(content) * 4

    debug('mc.__load_content_u32: segment=%s, base=%s, size=%s, sp=%s', segment, base, size, sp)

    for i in content:
      self.write_u32(sp, i.u32, privileged = True)
      sp += 4

  def load_text(self, segment, base, content):
    debug('mc.load_text: segment=%s, base=%s', segment, base)

    self.__load_content_u32(segment, base, content)

  def load_data(self, segment, base, content):
    debug('mc.load_data: segment=%s, base=%s', segment, base)

    self.__load_content_u8(segment, base, content)

  def __set_section_flags(self, pages_start, pages_cnt, flags):
    debug('__set_section_flags: start=%s, cnt=%s, flags=%s', pages_start, pages_cnt, flags)

    self.reset_pages_flags(pages_start, pages_cnt)
    self.update_pages_flags(pages_start, pages_cnt, 'read', flags.readable == 1)
    self.update_pages_flags(pages_start, pages_cnt, 'write', flags.writable == 1)
    self.update_pages_flags(pages_start, pages_cnt, 'execute', flags.executable == 1)

  def load_file(self, file_in, csr = None, dsr = None, stack = True):
    debug('mc.load_file: file_in=%s, csr=%s, dsr=%s', file_in, csr, dsr)

    from . import binary

    # One segment for code and data
    csr = csr or self.alloc_segment()
    dsr = dsr or csr
    sp  = None
    ip  = None

    symbols = {}
    regions = []

    with binary.File(file_in, 'r') as f_in:
      f_in.load()

      f_header = f_in.get_header()

      for i in range(0, f_header.sections):
        s_header, s_content = f_in.get_section(i)

        debug('loading section %s', f_in.string_table.get_string(s_header.name))

        s_base_addr = None

        if s_header.type == binary.SectionTypes.SYMBOLS:
          for symbol in s_content:
            symbols[f_in.string_table.get_string(symbol.name)] = UInt16(symbol.address)

          continue

        if s_header.type == binary.SectionTypes.STRINGS:
          continue

        s_base_addr = segment_addr_to_addr(csr if s_header.type == binary.SectionTypes.TEXT else dsr, s_header.base)
        pages_start, pages_cnt = area_to_pages(s_base_addr, s_header.file_size)

        if f_header.flags.mmapable == 1:
          access = ''
          if s_header.flags.readable == 1:
            access += 'r'
          if s_header.flags.writable == 1:
            access += 'w'
          if s_header.flags.executable == 1:
            access += 'x'

          self.mmap_area(f_in.name, s_base_addr, s_header.file_size, offset = s_header.offset, access = access, shared = False)

        else:
          self.for_each_page(pages_start, pages_cnt, self.__alloc_page)

          if s_header.type == binary.SectionTypes.TEXT:
            self.load_text(csr, s_header.base, s_content)

          elif s_header.type == binary.SectionTypes.DATA:
            if s_header.flags.bss != 1:
              self.load_data(dsr, s_header.base, s_content)

        self.__set_section_flags(pages_start, pages_cnt, s_header.flags)

        regions.append(MemoryRegion(f_in.string_table.get_string(s_header.name), s_base_addr, s_header.file_size, s_header.flags))

    if stack:
      pg, sp = self.alloc_stack(segment = dsr)
      regions.append(MemoryRegion('stack', pg.base_address, PAGE_SIZE, binary.SectionFlags.create(True, True, False, False, False)))

    return (csr, dsr, sp, ip, symbols, regions, f_in)

  def __get_mmap_fileno(self, file_path):
    if file_path not in self.opened_mmap_files:
      self.opened_mmap_files[file_path] = [0, open(file_path, 'r+b')]

    desc = self.opened_mmap_files[file_path]

    desc[0] += 1
    return desc[1].fileno()

  def __put_mmap_fileno(self, file_path):
    desc = self.opened_mmap_files[file_path]

    desc[0] -= 1
    if desc[0] > 0:
      return

    desc[1].close()
    del self.opened_mmap_files[file_path]

  def mmap_area(self, file_path, address, size, offset = 0, access = 'r', shared = False):
    """
    Assign set of memory pages to mirror external file, mapped into memory.

    :param string file_path: path of external file, whose content new area
      should reflect.
    :param u24 address: address where new area should start.
    :param u24 size: length of area, in bytes.
    :param int offset: starting point of the area in mmaped file.
    :param string access: combination of letters ``r`` (`read`), ``w``
      (`write`) and ``x`` (`execute`), specifying access flags of pages in new
      area.
    :param bool shared: if ``True``, content of external file is mmaped as
      shared, i.e. all changes are visible to all processes, not only to the
      current ducky virtual machine.
    :returns: newly created mmap area.
    :rtype: ducky.mm.MMapArea
    :raises ducky.errors.InvalidResourceError: when ``size`` is not multiply of
      :py:data:`ducky.mm.PAGE_SIZE`, or when ``address`` is not multiply of
      :py:data:`ducky.mm.PAGE_SIZE`, or when any of pages in the affected area
      is already allocated.
    """

    debug('mc.mmap_area: file=%s, offset=%s, size=%s, address=%s, access=%s, shared=%s', file_path, offset, size, address, access, shared)

    if size % PAGE_SIZE != 0:
      raise InvalidResourceError('Memory size must be multiple of PAGE_SIZE')

    if address % PAGE_SIZE != 0:
      raise InvalidResourceError('MMap area address must be multiple of PAGE_SIZE')

    pages_start, pages_cnt = area_to_pages(address, size)

    def __assert_page_missing(page_index, area_index):
      if page_index in self.__pages:
        raise InvalidResourceError('MMap request overlaps with existing pages')

    self.for_each_page(pages_start, pages_cnt, __assert_page_missing)

    access = access.lower()

    mmap_flags = mmap.MAP_SHARED if shared else mmap.MAP_PRIVATE

    mmap_prot = 0
    if 'r' in access or 'x' in access:
      mmap_prot |= mmap.PROT_READ
    if 'w' in access:
      mmap_prot |= mmap.PROT_WRITE

    ptr = mmap.mmap(
      self.__get_mmap_fileno(file_path),
      size,
      flags = mmap_flags,
      prot = mmap_prot,
      offset = offset)

    def __create_mmap_page(page_index, area_index):
      self.__pages[page_index] = MMapMemoryPage(self, page_index, ptr, area_index * PAGE_SIZE)

    self.for_each_page(pages_start, pages_cnt, __create_mmap_page)

    self.reset_pages_flags(pages_start, pages_cnt)

    if 'r' in access:
      self.update_pages_flags(pages_start, pages_cnt, 'read', True)
    if 'w' in access:
      self.update_pages_flags(pages_start, pages_cnt, 'write', True)
    if 'x' in access:
      self.update_pages_flags(pages_start, pages_cnt, 'execute', True)

    return MMapArea(address, size, file_path, ptr, pages_start, pages_cnt)

  def unmmap_area(self, mmap_area):
    self.reset_pages_flags(mmap_area.pages_start, mmap_area.pages_cnt)

    def __remove_mmap_page(page_index, _):
      del self.__pages[page_index]

    self.for_each_page(mmap_area.pages_start, mmap_area.pages_cnt, __remove_mmap_page)

    del self.mmap_areas[mmap_area.address]

    mmap_area.ptr.close()

    self.__put_mmap_fileno(mmap_area.file_path)

  def cas_u16(self, addr, test, rep):
    page = self.get_page((addr & PAGE_MASK) >> PAGE_SHIFT)

    v = page.read_u16(addr & (PAGE_SIZE - 1))
    if v == test.u16:
      v = rep.u16
      return True
    return v

  def read_u8(self, addr, privileged = False):
    debug('mc.read_u8: addr=%s, priv=%i', addr, privileged)

    return self.get_page((addr & PAGE_MASK) >> PAGE_SHIFT).read_u8(addr & (PAGE_SIZE - 1), privileged = privileged)

  def read_u16(self, addr, privileged = False):
    debug('mc.read_u16: addr=%s, priv=%i', addr, privileged)

    if self.force_aligned_access and addr % 2:
      raise AccessViolationError('Unable to access unaligned address: addr={}'.format(ADDR_FMT(addr)))

    return self.get_page((addr & PAGE_MASK) >> PAGE_SHIFT).read_u16(addr & (PAGE_SIZE - 1), privileged = privileged)

  def read_u32(self, addr, privileged = False):
    debug('mc.read_u32: addr=%s, priv=%i', addr, privileged)

    if self.force_aligned_access and addr % 4:
      raise AccessViolationError('Unable to access unaligned address: addr={}'.format(ADDR_FMT(addr)))

    return self.get_page((addr & PAGE_MASK) >> PAGE_SHIFT).read_u32(addr & (PAGE_SIZE - 1), privileged = privileged)

  def write_u8(self, addr, value, privileged = False, dirty = True):
    debug('mc.write_u8: addr=%s, value=%s, priv=%i, dirty=%i', addr, value, privileged, dirty)
    self.get_page((addr & PAGE_MASK) >> PAGE_SHIFT).write_u8(addr & (PAGE_SIZE - 1), value, privileged = privileged, dirty = dirty)

  def write_u16(self, addr, value, privileged = False, dirty = True):
    debug('mc.write_u16: addr=%s, value=%s, priv=%i, dirty=%i', addr, value, privileged, dirty)

    if self.force_aligned_access and addr % 2:
      raise AccessViolationError('Unable to access unaligned address: addr={}'.format(ADDR_FMT(addr)))

    self.get_page((addr & PAGE_MASK) >> PAGE_SHIFT).write_u16(addr & (PAGE_SIZE - 1), value, privileged = privileged, dirty = dirty)

  def write_u32(self, addr, value, privileged = False, dirty = True):
    debug('mc.write_u32: addr=%s, value=%s, priv=%i, dirty=%i', addr, value, privileged, dirty)

    if self.force_aligned_access and addr % 4:
      raise AccessViolationError('Unable to access unaligned address: addr={}'.format(ADDR_FMT(addr)))

    self.get_page((addr & PAGE_MASK) >> PAGE_SHIFT).write_u32(addr & (PAGE_SIZE - 1), value, privileged = privileged, dirty = dirty)

  def save_interrupt_vector(self, table, index, desc):
    """
    save_interrupt_vector(mm.UInt24, int, cpu.InterruptVector)
    """

    from ..cpu import InterruptVector

    debug('mc.save_interrupt_vector: table=%s, index=%i, desc=(CS=%s, DS=%s, IP=%s)',
          table, index, desc.cs, desc.ds, desc.ip)

    vector_address = table + index * sizeof(InterruptVector)

    self.write_u8(vector_address, desc.cs, privileged = True)
    self.write_u8(vector_address + 1, desc.ds, privileged = True)
    self.write_u16(vector_address + 2, desc.ip, privileged = True)

  def load_interrupt_vector(self, table, index):
    """
    load_interrupt_vector(int, int)
    """

    from ..cpu import InterruptVector

    debug('mc.load_interrupt_vector: table=%s, index=%i', table, index)

    desc = InterruptVector()

    vector_address = table + index * sizeof(InterruptVector)

    desc.cs = self.read_u8(vector_address, privileged = True)
    desc.ds = self.read_u8(vector_address + 1, privileged = True)
    desc.ip = self.read_u16(vector_address + 2, privileged = True)

    return desc
