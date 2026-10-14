/* link.ld plus the GNU build ID kept in flash, as an STM32 linker script would place it
   (link with -Wl,--build-id). RDT reads it from the device and compares it with the ELF (SYM-8). */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 16K
}

SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .note.gnu.build-id : { KEEP(*(.note.gnu.build-id)) } > FLASH
  .text   : { *(.text*) }   > FLASH
  .rodata : { *(.rodata*) } > FLASH
  .data   : { *(.data*) }   > RAM AT > FLASH
  .bss    : { *(.bss*) *(COMMON) } > RAM
}
