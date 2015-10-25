import os
import sys

if os.environ.get('DUCKY_IMPORT_DEVEL', 'no') == 'yes':
  sys.path.insert(0, os.environ.get('TOPDIR'))

import logging
import tempfile
import types

import ducky.patch
import ducky.config
import ducky.cpu.assemble
import ducky.cpu.registers
import ducky.console
import ducky.machine
import ducky.mm
import ducky.snapshot
import ducky.util

def get_tempfile():
  return tempfile.NamedTemporaryFile('w+b', delete = False, dir = os.path.join(os.getenv('TESTSETDIR'), 'tmp'))

def prepare_file(size, messages = None, pattern = 0xDE):
  f_tmp = get_tempfile()

  # fill file with pattern
  f_tmp.seek(0)
  for _ in range(0, size):
    f_tmp.write(chr(pattern))

  messages = messages or []

  # write out messages
  for offset, msg in messages:
    f_tmp.seek(offset)
    f_tmp.write(msg)

  f_tmp.close()

  return f_tmp

def assert_registers(state, **regs):
  for reg in ducky.cpu.registers.REGISTER_NAMES:
    if reg in ('flags', 'ip', 'cnt'):
      continue

    default = 0
    if reg in ('fp', 'sp'):
      default = 0x11F4 if os.environ.get('MMAPABLE_SECTIONS', 'no') == 'yes' else 0x02F4

    elif reg in ('cs', 'ds'):
      default = 0x01

    val = regs.get(reg, default)

    reg_index = ducky.cpu.registers.REGISTER_NAMES.index(reg)
    reg_value = state.registers[reg_index]

    assert reg_value == val, 'Register {} expected to have value {} ({}), {} ({}) found instead'.format(reg, ducky.mm.UINT16_FMT(val), val, ducky.mm.UINT16_FMT(reg_value), reg_value)

def assert_flags(state, **flags):
  real_flags = ducky.cpu.registers.FlagsRegister.from_uint16(state.registers[ducky.cpu.registers.Registers.FLAGS])

  assert real_flags.privileged == flags.get('privileged', 1), 'PRIV flag expected to be {}'.format(flags.get('privileged', 1))
  assert real_flags.hwint == flags.get('hwint', 1), 'HWINT flag expected to be {}'.format(flags.get('hwint', 1))
  assert real_flags.e == flags.get('e', 0), 'E flag expected to be {}'.format(flags.get('e', 0))
  assert real_flags.z == flags.get('z', 0), 'Z flag expected to be {}'.format(flags.get('z', 0))
  assert real_flags.o == flags.get('o', 0), 'O flag expected to be {}'.format(flags.get('o', 0))
  assert real_flags.s == flags.get('s', 0), 'S flag expected to be {}'.format(flags.get('s', 0))

def assert_mm(state, **cells):
  for addr, expected_value in cells.items():
    addr = ducky.util.str2int(addr)
    expected_value = ducky.util.str2int(expected_value)
    page_index = ducky.mm.addr_to_page(addr)
    page_offset = ducky.mm.addr_to_offset(addr)

    for page in state.get_page_states():
      if page.index != page_index:
        continue

      real_value = page.content[page_offset] | (page.content[page_offset + 1] << 8)
      assert real_value == expected_value, 'Value at {} (page {}, offset {}) should be {}, {} found instead'.format(ducky.mm.ADDR_FMT(addr), page_index, ducky.mm.UINT8_FMT(page_offset), ducky.mm.UINT16_FMT(expected_value), ducky.mm.UINT16_FMT(real_value))
      break

    else:
      assert False, 'Page {} (address {}) not found in memory'.format(page_index, ducky.mm.ADDR_FMT(addr))

def assert_mm_pages(state, *pages):
  for pg_id in pages:
    for pg_state in state.get_page_states():
      if pg_state.index == pg_id:
        break
    else:
      assert False, 'Page {} not found in VM state'.format(pg_id)

def assert_file_content(filename, cells):
  with open(filename, 'rb') as f:
    for cell_offset, cell_value in cells.iteritems():
      f.seek(cell_offset)
      real_value = ord(f.read(1))
      assert real_value == cell_value, 'Value at {} (file {}) should be {}, {} found instead'.format(cell_offset, filename, ducky.mm.UINT8_FMT(cell_value), ducky.mm.UINT8_FMT(real_value))

def compile_code(code):
  f_asm = get_tempfile()
  print f_asm
  f_asm.write(code)
  print f_asm
  f_asm.flush()
  print f_asm
  f_asm.close()
  print f_asm

  f_obj_name = os.path.splitext(f_asm.name)[0] + '.o'
  f_bin_name = os.path.splitext(f_asm.name)[0] + '.testbin'

  os.system('PYTHONPATH={} {} -f -I {} -i {} -o {}'.format(os.getenv('PYTHONPATH'), os.path.join(os.getenv('TOPDIR'), 'tools', 'as'), os.getenv('TOPDIR'), f_asm.name, f_obj_name))
  os.system('PYTHONPATH={} {} -f -i {} -o {} --section-base=.text=0x0000'.format(os.getenv('PYTHONPATH'), os.path.join(os.getenv('TOPDIR'), 'tools', 'ld'), f_obj_name, f_bin_name))

  os.unlink(f_asm.name)
  os.unlink(f_obj_name)

  return f_bin_name

def run_machine(code = None, binary = None, machine_config = None, coredump_file = None, post_boot = None, post_run = None):
  post_boot = post_boot or []
  post_run = post_run or []

  M = ducky.machine.Machine()

  if os.getenv('VMDEBUG') == 'yes':
    M.LOGGER.setLevel(logging.DEBUG)

  if os.getenv('DEBUG_OPEN_FILES') == 'yes':
    file_open_patcher = ducky.util.FileOpenPatcher(M.LOGGER)
    file_open_patcher.patch()

  if code is not None:
    binary_path = compile_code(code)

  else:
    binary_path = binary

  machine_config.add_section('binary-0')
  machine_config.set('binary-0', 'file', binary_path)

  M.hw_setup(machine_config)
  M.boot()

  for fn in post_boot:
    fn(M)

  M.run()

  if os.getenv('DEBUG_OPEN_FILES') == 'yes':
    if file_open_patcher.has_open_files():
      file_open_patcher.log_open_files()
      assert False

    file_open_patcher.restore()

  if code is not None:
    os.unlink(binary_path)

  for fn in post_run:
    fn(M, M.last_state)

def common_run_machine(code = None, binary = None, machine_config = None, cpus = 1, cores = 1, irq_routines = 'tests/instructions/interrupts-basic', post_boot = None, post_run = None):
  if code is not None and isinstance(code, types.ListType):
    code = '\n'.join(code)

  if machine_config is None:
    machine_config = ducky.config.MachineConfig()

  machine_config.add_section('machine')
  machine_config.set('machine', 'cpus', cpus)
  machine_config.set('machine', 'cores', cores)
  machine_config.set('machine', 'interrupt-routines', os.path.join(os.getenv('CURDIR'), irq_routines))
  machine_config.add_section('cpu')
  machine_config.set('cpu', 'math-coprocessor', 'yes')

  run_machine(code = code, binary = binary, machine_config = machine_config, post_boot = post_boot, post_run = post_run)