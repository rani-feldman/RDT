#!/bin/sh
# Rebuilds the test ELFs. Needs clang and ld.lld; the outputs are committed,
# so this only runs when the sources change.
set -e
cd "$(dirname "$0")"
for fw in main motorctl; do
    clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 \
        -ffreestanding -nostdlib -g -O1 -fuse-ld=lld -Wl,-T,link.ld \
        -Wl,--entry=main -Wl,-z,max-page-size=16 -o "$fw.elf" "$fw.c"
done
