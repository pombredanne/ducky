main:
  li r0, 0xFF
  cmp r0, 0x1FF
  bge &label
  li r0, 0xEE
label:
  int 0