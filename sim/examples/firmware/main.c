/* Test firmware for the main MCU (MCU 0). Built only to give the simulator an ELF. */
#include <stdint.h>

typedef enum { MODE_IDLE = 0, MODE_RUN = 1, MODE_FAULT = 2 } mode_t;

typedef struct {
    float    speed;
    float    target;
    int16_t  current_ma;
    uint8_t  enabled;
    mode_t   mode;
} motor_t;

motor_t motor = { 1.5f, 3000.0f, -120, 1, MODE_RUN };
volatile uint32_t tick_count = 0x12345678;
uint8_t  rx_buffer[200];
const char firmware_name[16] = "rdt-main";
int32_t  gains[4] = { 10, -20, 30, -40 };

int main(void)
{
    for (;;) {
        tick_count++;
        motor.speed += 0.1f;
        rx_buffer[tick_count & 0x7F] = (uint8_t)gains[tick_count & 3];
        if (firmware_name[0] == 0) break;
    }
    return 0;
}
