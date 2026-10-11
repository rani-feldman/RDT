/* Second translation unit, for file-static names that clash. */
#include "corpus.h"

static volatile uint8_t shared_name = 0x22;
static volatile point_t origin = { -1, 1 };

uint8_t other_value(void) { return shared_name + (uint8_t)origin.x; }
