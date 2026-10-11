/* Minimal Cortex-M style layout: code and constants in flash, data and bss in RAM. */
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
}
