;
; Instruction sets
;
.def INST_SET_DUCKY: 0
.def INST_SET_MATH:  1


;
; Memory
;

.def PAGE_SIZE: 256

;
; Interrupts
;
.def INT_HALT:    0
.def INT_BLOCKIO: 1
.def INT_VMDEBUG: 2
.def INT_CONIO:   3
.def INT_MM:      4
.def INT_MATH:    5


;
; VMDebug Operations
;
.def VMDEBUG_QUIET:  0


;
; ConsoleIO Operations
;
.def CONIO_ECHO:    0


;
; BlockIO Operations
;
.def BLOCKIO_READ:  0
.def BLOCKIO_WRITE: 1


;
; Math Coprocessor Operations
;
.def MATH_LTOII:  9
.def MATH_INCL:   0
.def MATH_DECL:   1
.def MATH_ADDL:   2
.def MATH_SUBL:   3
.def MATH_MULL:   4
.def MATH_DIVL:   5
.def MATH_MODL:   6
.def MATH_ITOL:   7
.def MATH_LTOI:   8
.def MATH_IITOL: 10
.def MATH_DUPL:  11
.def MATH_UTOL:  12
.def MATH_SYMDIVL: 13
.def MATH_SYMMODL: 14


;
; IO ports
;

; Console IO
.def PORT_CONIO_STDIN:  0x100
.def PORT_CONIO_STDOUT: 0x100
.def PORT_CONIO_STDERR: 0x101
