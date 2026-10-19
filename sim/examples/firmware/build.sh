#!/bin/sh
# Rebuilds the test ELFs. Needs clang and ld.lld; the outputs are committed,
# so this only runs when the sources change.
set -e
cd "$(dirname "$0")"
CC="clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 \
    -ffreestanding -nostdlib -g -fuse-ld=lld -Wl,--entry=main -Wl,-z,max-page-size=16"
# main.elf also carries main_data.c: structures, arrays and arrays of structures.
$CC -O1 -Wl,-T,link.ld -o main.elf main.c main_data.c
$CC -O1 -Wl,-T,link.ld -o motorctl.elf motorctl.c
# main.c with a GNU build ID in flash (SYM-8), and a second build of it that differs (-O0).
$CC -O1 -Wl,-T,link_build_id.ld -Wl,--build-id=sha1 -o main_build_id.elf main.c
$CC -O0 -Wl,-T,link_build_id.ld -Wl,--build-id=sha1 -o main_build_id_v2.elf main.c
