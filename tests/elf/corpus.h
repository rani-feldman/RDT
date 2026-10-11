/* Types shared by the symbol test corpus. */
#include <stdint.h>
#include <stdbool.h>

typedef enum { STATE_OFF = 0, STATE_ON = 1, STATE_ERROR = -1 } state_t;

typedef struct {
    uint32_t ready   : 1;
    uint32_t mode    : 3;
    int32_t  level   : 5;   /* signed bit field */
    uint32_t         : 7;   /* unnamed padding */
    uint32_t channel : 16;
} flags_t;

typedef struct point { int16_t x; int16_t y; } point_t;

typedef struct node {
    int32_t      value;
    struct node *next;
} node_t;

typedef struct {
    char         name[12];
    point_t      corners[2];
    flags_t      flags;
    state_t      state;
    union {
        uint32_t word;
        uint8_t  bytes[4];
        float    real;
    } raw;
    struct {                /* anonymous struct member: its fields belong to the parent */
        uint16_t lo;
        uint16_t hi;
    };
    double       gain;
    int64_t      total;
    bool         armed;
} device_t;

typedef void (*handler_t)(void);
typedef volatile const uint32_t ro_reg_t;
