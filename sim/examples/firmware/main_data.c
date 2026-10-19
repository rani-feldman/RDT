/* Structures, arrays and arrays of structures for the main MCU (MCU 0), so the simulator has
   something to show in RDT's variable tree (rani, 2026-10-19). Linked into main.elf with main.c.

   The pragma puts these variables in their own sections, which link.ld places after main.c's,
   so main.c's variables keep the addresses the tests and examples use: RAM ones from
   0x20000400, the constant table after main.c's constants in flash. */
#include <stdint.h>

#pragma clang section bss = ".demo.bss" data = ".demo.data" rodata = ".demo.rodata"

typedef enum { CH_OFF = 0, CH_VOLTAGE = 1, CH_CURRENT = 2, CH_TEMP = 3 } channel_kind_t;

/* A structure inside a structure. */
typedef struct {
    float x;
    float y;
    float z;
} vec3_t;

typedef struct {
    vec3_t   accel;          /* m/s^2 */
    vec3_t   gyro;           /* deg/s */
    int16_t  temp_c10;       /* 0.1 degC */
    uint8_t  status;
    uint8_t  sample_rate_hz;
} imu_t;

/* A structure with an array inside it, used for an array of structures. */
typedef struct {
    uint8_t        id;
    channel_kind_t kind;
    float          setpoint;
    float          measured;
    uint16_t       history[8];
    uint32_t       error_count;
} channel_t;

/* A structure with bit fields, inside a structure with an array of structures. */
typedef struct {
    uint32_t enabled  : 1;
    uint32_t fault    : 1;
    uint32_t mode     : 3;
    uint32_t reserved : 27;
} flags_t;

typedef struct {
    char     name[12];
    flags_t  flags;
    channel_t channel[2];
} board_t;

/* A constant table of structures, kept in flash (read-only on the device). */
typedef struct {
    int16_t  min;
    int16_t  max;
    uint16_t step;
} limit_t;

/* Structures */
imu_t imu = {
    { 0.0f, 0.0f, 9.81f },
    { 0.0f, 0.0f, 0.0f },
    235, 1, 100,
};

/* Arrays */
float    pid_gains[3]      = { 1.2f, 0.05f, 0.01f };                 /* Kp, Ki, Kd */
int16_t  adc_samples[16];                                            /* zeroed, in .bss */
uint16_t lookup[4][4]      = { { 0, 1, 2, 3 }, { 10, 11, 12, 13 },   /* two dimensions */
                               { 20, 21, 22, 23 }, { 30, 31, 32, 33 } };
char     device_name[16]   = "sim-bench";

/* Arrays of structures */
channel_t channels[4] = {
    { 0, CH_VOLTAGE, 12.0f, 0.0f, { 1198, 1201, 1203, 1199, 1200, 1202, 1197, 1200 }, 0 },
    { 1, CH_CURRENT,  2.5f, 0.0f, { 248, 251, 250, 252, 249, 250, 247, 251 }, 0 },
    { 2, CH_TEMP,    40.0f, 0.0f, { 395, 397, 398, 400, 401, 403, 404, 405 }, 0 },
    { 3, CH_OFF,      0.0f, 0.0f, { 0 }, 0 },
};
vec3_t waypoints[3] = { { 0.0f, 0.0f, 0.0f }, { 1.0f, 2.0f, 0.5f }, { -1.5f, 4.0f, 1.0f } };
board_t boards[2] = {
    { "left",  { 1, 0, 2, 0 }, { { 0, CH_VOLTAGE, 5.0f, 0.0f, { 0 }, 0 },
                                 { 1, CH_CURRENT, 1.0f, 0.0f, { 0 }, 0 } } },
    { "right", { 0, 0, 0, 0 }, { { 0, CH_VOLTAGE, 3.3f, 0.0f, { 0 }, 0 },
                                 { 1, CH_TEMP,   25.0f, 0.0f, { 0 }, 0 } } },
};
const limit_t limits[4] = { { -100, 100, 1 }, { 0, 3000, 10 }, { -40, 125, 1 }, { 0, 255, 5 } };
