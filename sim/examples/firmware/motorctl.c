/* Test firmware for a secondary MCU (MCU 1). Built only to give the simulator an ELF. */
#include <stdint.h>

typedef struct {
    uint16_t samples[64];   /* 128 bytes: larger than one 64-byte internal frame */
    uint32_t count;
} adc_log_t;

adc_log_t adc_log = { { 1, 2, 3, 4, 5, 6, 7, 8 }, 8 };
volatile uint32_t pwm_duty = 500;
float    temperature = 25.5f;

int main(void)
{
    for (;;) {
        adc_log.samples[adc_log.count & 63] = (uint16_t)pwm_duty;
        adc_log.count++;
        if (temperature > 100.0f) break;
    }
    return 0;
}
