/* Minimal Cortex-M style layout: code and constants in flash, data and bss in RAM.
   main_data.c's variables (.demo.*) come after main.c's so that main.c's addresses stay put. */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 16K
}

SECTIONS
{
  .text   : { *(.text*) }   > FLASH
  .rodata : { *(.rodata*) } > FLASH
  .data   : { *(.data*) }   > RAM AT > FLASH
  .bss    : { *(.bss*) *(COMMON) } > RAM
  .demo.rodata : { *(.demo.rodata) } > FLASH
  .demo.data 0x20000400 : { *(.demo.data) } > RAM AT > FLASH
  .demo.bss  : { *(.demo.bss) } > RAM
}
