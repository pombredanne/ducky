  .text

  li r0, 0x1FF
  cmp r0, 0xFF
  bg &label
  li r0, 0xEE
label:
  hlt 0x00
