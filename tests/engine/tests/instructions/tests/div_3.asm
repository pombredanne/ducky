  .include "defs.asm"
main:
  li r0, 0
  li r1, 2
  div r0, r1
  int $INT_HALT