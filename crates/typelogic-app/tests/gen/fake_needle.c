/* A stand-in for libneedle3 with the same C ABI, for the FFI test of needle.rs (cargo test -- --ignored).
   cc -shared -fPIC -o libneedle3.so fake_needle.c */
#include <stdint.h>
#include <string.h>
#include <stdio.h>
static int loaded = 0, inits = 0, resets = 0;
static char tools_seen[4096];
int needle_load(const char *data, uint64_t len) { loaded = (len == 8 && memcmp(data, "WEIGHTS!", 8) == 0); return loaded ? 0 : 1; }
int needle_init(const char *system, const char *tools, const char *unused) {
    (void)unused; inits++; snprintf(tools_seen, sizeof tools_seen, "%s|%s", system, tools); return strstr(tools, "record_fields") ? 0 : -1;
}
void needle_reset(void) { resets++; }
int needle_complete(const char *text, float *probs, int n, int max_tokens, char *out, int size) {
    (void)probs; (void)n; (void)max_tokens;
    if (size < 200000) { out[0] = 0; return -2; }   /* asks for a larger buffer once */
    snprintf(out, size, "{\"type\": \"result\", \"function_calls\": [{\"name\": \"record_fields\", \"arguments\": "
             "{\"amount\": 12.5, \"days\": 3.0, \"kind\": \"%s\", \"missing\": \"\"}}], \"confidence\": 0.912345, "
             "\"reasoning\": \"inits=%d resets=%d\"}", strstr(text, "refund") ? "refund" : "other", inits, resets);
    return 0;
}
