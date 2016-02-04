  .data

  .type stack, space
  .space 64

  .text

  li r0, 88
  la r1, &irq_routine
  stw r0, r1

  add r0, 4
  la r1, &stack
  add r1, 64
  stw r0, r1

  li r1, 0xFF
  int 11
  hlt 0x01


irq_routine:
  cli
  hlt 0x00
