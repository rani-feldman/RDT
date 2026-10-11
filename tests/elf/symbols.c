/* Symbol test corpus: one of every kind of global the symbol reader must handle.
   Rebuilt by build.sh; the ELFs are committed. CORPUS_V2 is a "rebuilt" firmware for the
   reload tests: everything moves, counter changes type and opaque is gone. */
#include "corpus.h"

#ifdef CORPUS_V2
uint32_t   added_first[4] = { 1, 2, 3, 4 };
#endif

device_t   dev = {
    .name = "pump", .corners = { { 1, -2 }, { 300, -400 } },
    .flags = { .ready = 1, .mode = 5, .level = -3, .channel = 0xBEEF },
    .state = STATE_ON, .raw = { .word = 0x3FC00000 }, .lo = 7, .hi = 9,
    .gain = 0.25, .total = -5000000000LL, .armed = true,
};
node_t     nodes[3] = { { 10, &nodes[1] }, { 20, &nodes[2] }, { 30, 0 } };
node_t    *head = &nodes[0];
node_t   **head_ref = &head;
int16_t    matrix[2][3] = { { 1, 2, 3 }, { 4, 5, 6 } };
const char banner[] = "corpus v1";
const char *message = banner;
#ifdef CORPUS_V2
volatile uint16_t counter = 42;
#else
volatile uint32_t counter = 42;
#endif
ro_reg_t   version = 0x00010203;
state_t    states[2] = { STATE_OFF, STATE_ERROR };
handler_t  on_tick;
#ifndef CORPUS_V2
void      *opaque = &dev;
#endif
static volatile uint8_t shared_name = 0x11;   /* other.c has its own shared_name */
uint8_t    zeroed[300];

void tick(void) { counter++; }

int main(void)
{
    on_tick = tick;
    for (;;) {
        on_tick();
        zeroed[counter % 300] = shared_name;
        if (head == 0) break;
    }
    return 0;
}
