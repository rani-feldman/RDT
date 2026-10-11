#!/bin/sh
# Rebuilds the symbol test ELFs. The outputs are committed, so this only runs when the sources change.
#   corpus-clang.elf    clang, Cortex-M4, little endian, DWARF 4
#   corpus-clang-be.elf clang, ARMv7 big endian, DWARF 5
#   corpus-gcc.elf      host gcc, x86-64 (64-bit pointers), GCC's DWARF 5
#   corpus-clang-v2.elf as corpus-clang.elf, "rebuilt" with CORPUS_V2 for the reload tests
set -e
cd "$(dirname "$0")"
SRC="symbols.c other.c"
clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 \
    -ffreestanding -nostdlib -g -gdwarf-4 -O1 -fuse-ld=lld -Wl,-T,link.ld \
    -Wl,--entry=main -Wl,-z,max-page-size=16 -o corpus-clang.elf $SRC
clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 \
    -ffreestanding -nostdlib -g -gdwarf-4 -O1 -fuse-ld=lld -Wl,-T,link.ld \
    -Wl,--entry=main -Wl,-z,max-page-size=16 -DCORPUS_V2 -o corpus-clang-v2.elf $SRC
clang --target=armebv7r-none-eabi -ffreestanding -nostdlib -g -gdwarf-5 -O1 -fuse-ld=lld -Wl,-T,link.ld \
    -Wl,--entry=main -Wl,-z,max-page-size=16 -o corpus-clang-be.elf $SRC
gcc -ffreestanding -nostdlib -static -fno-pie -no-pie -g -O1 -fno-asynchronous-unwind-tables \
    -Wl,-T,link.ld -Wl,--entry=main -Wl,--build-id=none -o corpus-gcc.elf $SRC
