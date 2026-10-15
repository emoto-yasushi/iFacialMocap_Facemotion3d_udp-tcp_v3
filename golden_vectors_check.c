/* FMV3 contract revision 6: check the answer key with standard C99 only.
 *
 *     cc -std=c99 -o golden_vectors_check golden_vectors.c golden_vectors_check.c
 *     ./golden_vectors_check
 *
 * For every vector in golden_vectors.c this program
 *   1. decodes the complete message, and the UDP datagrams joined in order, and compares
 *      what it reads with the expected fields (decoder side);
 *   2. encodes the message again from the expected fields only, as one message and as UDP
 *      datagrams, and compares the bytes with memcmp (encoder side).
 * Every number is read and written byte by byte as little-endian, so the result does not
 * depend on the host byte order, alignment or struct padding. No network is used.
 * It prints one line per vector and exits with status 1 if any byte or field differs.
 * To check your own code, use your decoder where this program calls check_*_body(), or
 * your encoder where it calls encode_*().
 */
#include <stdio.h>
#include <string.h>

#include "golden_vectors.h"

#define BUFFER_SIZE 8192   /* larger than the largest vector (3700 bytes) */

static const uint8_t magic[4] = { 0x46, 0x4d, 0x56, 0x33 };   /* "FMV3" */
static unsigned long differences;
static const char *current = "";   /* the vector being checked */
static const char *where = "";     /* "message" or "UDP" */

static void differ(const char *what)
{
    printf("  FAIL %s (%s): %s\n", current, where, what);
    differences++;
}

static void expect(int ok, const char *what)
{
    if (!ok)
        differ(what);
}

/* ---- Little-endian reads and writes, one byte at a time ---- */

static uint16_t get_u16(const uint8_t *p) { return (uint16_t)((unsigned)p[0] | (unsigned)p[1] << 8); }
static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get_u64(const uint8_t *p) { return (uint64_t)get_u32(p) | (uint64_t)get_u32(p + 4) << 32; }
static void put_u16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v & 0xFFu); p[1] = (uint8_t)(v >> 8); }
static void put_u32(uint8_t *p, uint32_t v)
{
    put_u16(p, (uint16_t)(v & 0xFFFFu));
    put_u16(p + 2, (uint16_t)(v >> 16));
}
static void put_u64(uint8_t *p, uint64_t v)
{
    put_u32(p, (uint32_t)(v & 0xFFFFFFFFu));
    put_u32(p + 4, (uint32_t)(v >> 32));
}
/* Signed values (two's complement) from their bits, with no implementation-defined conversion. */
static int32_t from_i16(uint16_t u) { return u < 0x8000u ? (int32_t)u : (int32_t)u - 65536L; }
static int32_t from_i32(uint32_t u)
{
    return u < 0x80000000u ? (int32_t)u : -(int32_t)(0xFFFFFFFFu - u) - 1;
}

static int same(const uint8_t *a, size_t a_length, const uint8_t *b, size_t b_length)
{
    return a_length == b_length && (a_length == 0 || memcmp(a, b, a_length) == 0);
}

/* ---- Decoder side: read the bytes and compare them with the expected fields ---- */

/* The header of the complete message (index 0, count 1) or of UDP datagram `index`. */
static void check_header(const fmv3_message_vector *m, const fmv3_bytes *bytes, size_t index, size_t count)
{
    const uint8_t *p = bytes->data;
    if (bytes->length < FMV3_HEADER_SIZE || memcmp(p, magic, 4) != 0) {
        differ("no FMV3 header");
        return;
    }
    expect(p[4] == m->message_type, "message_type");
    expect(p[5] == m->flags, "flags");
    expect(get_u16(p + 6) == FMV3_HEADER_SIZE, "header_size");
    expect(get_u64(p + 8) == m->session_id, "session_id");
    expect(get_u32(p + 16) == m->schema_id, "schema_id");
    expect(get_u32(p + 20) == m->sequence, "sequence");
    expect(get_u32(p + 24) == m->source_token, "source_token");
    expect(get_u32(p + 28) == m->body.length, "total_payload_length");
    expect(get_u16(p + 32) == index, "part_index");
    expect(get_u16(p + 34) == count, "part_count");
    expect(get_u32(p + 36) == bytes->length - FMV3_HEADER_SIZE, "chunk_length and the length of the bytes");
}

/* Join the bodies of the UDP datagrams in order. Each one except the last has exactly `capacity` bytes. */
static size_t join_udp(const fmv3_message_vector *m, size_t capacity, uint8_t *joined)
{
    size_t i, length = 0;
    expect(m->udp_count >= 1, "no UDP datagram");
    for (i = 0; i < m->udp_count; i++) {
        const fmv3_bytes *datagram = &m->udp[i];
        size_t chunk;
        check_header(m, datagram, i, m->udp_count);
        if (datagram->length < FMV3_HEADER_SIZE)
            return 0;
        chunk = datagram->length - FMV3_HEADER_SIZE;
        expect(chunk <= capacity, "UDP datagram larger than allowed");
        expect(i + 1 == m->udp_count || chunk == capacity, "UDP datagram not split at the fixed size");
        if (length + chunk > BUFFER_SIZE) {
            differ("body too long for this program");
            return 0;
        }
        memcpy(joined + length, datagram->data + FMV3_HEADER_SIZE, chunk);
        length += chunk;
    }
    return length;
}

static int read_string(const uint8_t *p, size_t length, size_t *at, size_t width, const fmv3_bytes *expected,
                       const char *what)
{
    size_t size;
    if (*at + width > length) {
        differ(what);
        return 0;
    }
    size = width == 2 ? get_u16(p + *at) : p[*at];
    *at += width;
    if (*at + size > length || !same(p + *at, size, expected->data, expected->length)) {
        differ(what);
        return 0;
    }
    *at += size;
    return 1;
}

/* A table: the SCHEMA body after the start information. */
static void check_layout(const fmv3_table *t, const uint8_t *p, size_t length)
{
    size_t at = FMV3_DESCRIPTOR_SIZE, i;
    if (length < FMV3_DESCRIPTOR_SIZE) {
        differ("table shorter than the descriptor");
        return;
    }
    expect(get_u16(p) == t->mode, "mode");
    expect(p[2] == t->blend_encoding, "blend_encoding");
    expect(p[3] == t->schema_format_version, "schema_format_version");
    if (get_u16(p + 4) != t->blend_count) {
        differ("blend_count");
        return;
    }
    if (t->mode == FMV3_MODE_FIXED52) {
        expect(length == FMV3_DESCRIPTOR_SIZE, "a fixed52 table has no strings");
        expect(t->blend_count == FMV3_FIXED52_COUNT && t->names == fmv3_fixed52_names, "fixed52 names");
        return;
    }
    /* Variable mode: app and profile with a UInt16 length, then the names with a UInt8 length. */
    if (!read_string(p, length, &at, 2, &t->app, "app") || !read_string(p, length, &at, 2, &t->profile, "profile"))
        return;
    for (i = 0; i < t->blend_count; i++) {
        if (!read_string(p, length, &at, 1, &t->names[i], "BlendShape name"))
            return;
    }
    expect(at == length, "the last name does not end at the end of the body");
}

static void check_hello_body(const fmv3_hello_vector *v, const uint8_t *body, size_t length)
{
    if (length != FMV3_HELLO_BODY_SIZE) {
        differ("HELLO body length");
        return;
    }
    expect(get_u16(body) == v->requested_fps, "requested_fps");
    expect(get_u16(body + 2) == v->max_udp_size, "max_udp_size");
    expect(get_u32(body + 4) == v->contract_revision, "contract_revision");
    expect(get_u16(body + 8) == v->requested_mode, "requested_mode");
}

static void check_control_body(const fmv3_control_vector *v, const uint8_t *body, size_t length)
{
    expect(same(body, length, v->text.data, v->text.length), "body (ERROR text)");
}

static void check_schema_body(const fmv3_schema_vector *v, const uint8_t *body, size_t length)
{
    if (length < FMV3_START_INFO_SIZE) {
        differ("SCHEMA body shorter than the start information");
        return;
    }
    expect(get_u64(body) == v->client_nonce, "client_nonce");
    expect(get_u16(body + 8) == v->actual_fps, "actual_fps");
    expect(get_u16(body + 10) == v->max_udp_size, "max_udp_size");
    expect(get_u32(body + 12) == v->lease_ms, "lease_ms");
    expect(get_u32(body + 16) == v->contract_revision, "contract_revision");
    check_layout(&v->table, body + FMV3_START_INFO_SIZE, length - FMV3_START_INFO_SIZE);
}

static void check_frame_body(const fmv3_frame_vector *v, const uint8_t *body, size_t length)
{
    const fmv3_table *t = &v->table;
    size_t size = t->blend_encoding == FMV3_ENCODING_I16 ? 2 : 4, i;
    char what[48];
    if (t->blend_encoding != FMV3_ENCODING_I16 && t->blend_encoding != FMV3_ENCODING_I32) {
        differ("blend_encoding");
        return;
    }
    if (length != t->blend_count * size + FMV3_POSE_SIZE) {
        differ("FRAME body length");
        return;
    }
    for (i = 0; i < t->blend_count; i++) {
        int32_t value = size == 2 ? from_i16(get_u16(body + 2 * i)) : from_i32(get_u32(body + 4 * i));
        if (value != v->values[i]) {
            snprintf(what, sizeof what, "BlendShape value %lu", (unsigned long)i);
            differ(what);
        }
    }
    for (i = 0; i < 12; i++) {
        /* Compare the Float32 bits, not a float or double value. */
        if (get_u32(body + t->blend_count * size + 4 * i) != v->pose_bits[i]) {
            snprintf(what, sizeof what, "head/eye Float32 %lu", (unsigned long)i);
            differ(what);
        }
    }
}

/* ---- Encoder side: build the bytes from the expected fields only ---- */

static size_t encode_hello(const fmv3_hello_vector *v, uint8_t *out)
{
    put_u16(out, v->requested_fps);
    put_u16(out + 2, v->max_udp_size);
    put_u32(out + 4, v->contract_revision);
    put_u16(out + 8, v->requested_mode);
    return FMV3_HELLO_BODY_SIZE;
}

static size_t put_string(uint8_t *out, size_t room, size_t at, size_t width, const fmv3_bytes *text)
{
    if (at + width + text->length > room)
        return room + 1;
    if (width == 2)
        put_u16(out + at, (uint16_t)text->length);
    else
        out[at] = (uint8_t)text->length;
    if (text->length > 0)
        memcpy(out + at + width, text->data, text->length);
    return at + width + text->length;
}

/* Returns the length, or more than `room` if it does not fit. */
static size_t encode_layout(const fmv3_table *t, uint8_t *out, size_t room)
{
    size_t at = FMV3_DESCRIPTOR_SIZE, i;
    put_u16(out, t->mode);
    out[2] = t->blend_encoding;
    out[3] = t->schema_format_version;
    put_u16(out + 4, t->blend_count);
    if (t->mode == FMV3_MODE_FIXED52)
        return at;
    at = put_string(out, room, at, 2, &t->app);
    if (at <= room)
        at = put_string(out, room, at, 2, &t->profile);
    for (i = 0; i < t->blend_count && at <= room; i++)
        at = put_string(out, room, at, 1, &t->names[i]);
    return at;
}

static size_t encode_schema(const fmv3_schema_vector *v, uint8_t *out)
{
    put_u64(out, v->client_nonce);
    put_u16(out + 8, v->actual_fps);
    put_u16(out + 10, v->max_udp_size);
    put_u32(out + 12, v->lease_ms);
    put_u32(out + 16, v->contract_revision);
    return FMV3_START_INFO_SIZE + encode_layout(&v->table, out + FMV3_START_INFO_SIZE,
                                                BUFFER_SIZE - FMV3_START_INFO_SIZE);
}

static size_t encode_frame(const fmv3_frame_vector *v, uint8_t *out)
{
    size_t size = v->table.blend_encoding == FMV3_ENCODING_I16 ? 2 : 4, at = 0, i;
    if (v->table.blend_count * size + FMV3_POSE_SIZE > BUFFER_SIZE)
        return BUFFER_SIZE + 1;
    for (i = 0; i < v->table.blend_count; i++, at += size) {
        if (size == 2)
            put_u16(out + at, (uint16_t)v->values[i]);   /* conversion to unsigned keeps the bits */
        else
            put_u32(out + at, (uint32_t)v->values[i]);
    }
    for (i = 0; i < 12; i++, at += 4)
        put_u32(out + at, v->pose_bits[i]);
    return at;
}

static void put_header(uint8_t *out, const fmv3_message_vector *m, size_t total, size_t index, size_t count,
                       size_t chunk)
{
    memcpy(out, magic, 4);
    out[4] = m->message_type;
    out[5] = m->flags;
    put_u16(out + 6, FMV3_HEADER_SIZE);
    put_u64(out + 8, m->session_id);
    put_u32(out + 16, m->schema_id);
    put_u32(out + 20, m->sequence);
    put_u32(out + 24, m->source_token);
    put_u32(out + 28, (uint32_t)total);
    put_u16(out + 32, (uint16_t)index);
    put_u16(out + 34, (uint16_t)count);
    put_u32(out + 36, (uint32_t)chunk);
}

/* Encode one message and the UDP datagrams (each `capacity` bytes of body, the last one the rest),
 * then compare them with the expected bytes. */
static void check_encoding(const fmv3_message_vector *m, const uint8_t *body, size_t length, size_t capacity)
{
    static uint8_t out[FMV3_HEADER_SIZE + BUFFER_SIZE];
    size_t count, i;
    where = "encoded";
    if (length > BUFFER_SIZE || capacity == 0) {
        differ("cannot encode");
        return;
    }
    put_header(out, m, length, 0, 1, length);
    memcpy(out + FMV3_HEADER_SIZE, body, length);
    expect(same(out, FMV3_HEADER_SIZE + length, m->message.data, m->message.length), "message bytes");
    count = length == 0 ? 1 : (length + capacity - 1) / capacity;
    expect(count == m->udp_count, "number of UDP datagrams");
    for (i = 0; i < count && i < m->udp_count; i++) {
        size_t chunk = length - i * capacity < capacity ? length - i * capacity : capacity;
        put_header(out, m, length, i, count, chunk);
        memcpy(out + FMV3_HEADER_SIZE, body + i * capacity, chunk);
        expect(same(out, FMV3_HEADER_SIZE + chunk, m->udp[i].data, m->udp[i].length), "UDP datagram bytes");
    }
}

/* ---- One vector: decode the message and the UDP datagrams, then encode ---- */

typedef void (*body_check)(const void *vector, const uint8_t *body, size_t length);

static void hello_body(const void *v, const uint8_t *b, size_t n) { check_hello_body(v, b, n); }
static void control_body(const void *v, const uint8_t *b, size_t n) { check_control_body(v, b, n); }
static void schema_body(const void *v, const uint8_t *b, size_t n) { check_schema_body(v, b, n); }
static void frame_body(const void *v, const uint8_t *b, size_t n) { check_frame_body(v, b, n); }

static void check_vector(const char *kind, const fmv3_message_vector *m, const void *vector, body_check check,
                         size_t capacity, const uint8_t *encoded, size_t encoded_length)
{
    static uint8_t joined[BUFFER_SIZE];
    unsigned long before = differences;
    size_t length;
    current = m->name;
    where = "message";
    check_header(m, &m->message, 0, 1);
    if (m->message.length >= FMV3_HEADER_SIZE) {
        expect(same(m->body.data, m->body.length, m->message.data + FMV3_HEADER_SIZE,
                    m->message.length - FMV3_HEADER_SIZE), "body is the message after the header");
        check(vector, m->message.data + FMV3_HEADER_SIZE, m->message.length - FMV3_HEADER_SIZE);
    }
    where = "UDP";
    length = join_udp(m, capacity, joined);
    check(vector, joined, length);
    check_encoding(m, encoded, encoded_length, capacity);
    printf("%s %-10s %-31s %4lu bytes, %lu UDP datagram(s)\n", differences == before ? "ok  " : "FAIL", kind,
           m->name, (unsigned long)m->message.length, (unsigned long)m->udp_count);
}

static void check_fixed52_names(void)
{
    size_t i;
    current = "fmv3_fixed52_names";
    where = "names";
    for (i = 0; i < FMV3_FIXED52_COUNT; i++) {
        const fmv3_bytes *a = &fmv3_fixed52_names[i];
        expect(a->length > 0, "empty name");
        if (i > 0 && a->length > 0) {
            const fmv3_bytes *previous = &fmv3_fixed52_names[i - 1];
            size_t n = previous->length < a->length ? previous->length : a->length;
            int order = memcmp(previous->data, a->data, n);
            expect(order < 0 || (order == 0 && previous->length < a->length), "not in alphabetical order");
        }
    }
}

int main(void)
{
    static uint8_t body[BUFFER_SIZE + 1];
    size_t i, length;
    printf("FMV3 contract revision %d answer key\n", FMV3_CONTRACT_REVISION);
    check_fixed52_names();
    for (i = 0; i < FMV3_HELLO_VECTOR_COUNT; i++) {
        const fmv3_hello_vector *v = &fmv3_hello_vectors[i];
        length = encode_hello(v, body);
        check_vector("HELLO", &v->m, v, hello_body, length, body, length);
    }
    for (i = 0; i < FMV3_CONTROL_VECTOR_COUNT; i++) {
        const fmv3_control_vector *v = &fmv3_control_vectors[i];
        length = v->text.length <= BUFFER_SIZE ? v->text.length : BUFFER_SIZE + 1;
        if (length > 0 && length <= BUFFER_SIZE)
            memcpy(body, v->text.data, length);
        check_vector(v->m.message_type == FMV3_ERROR ? "ERROR" : "control", &v->m, v, control_body,
                     length > 0 ? length : 1, body, length);
    }
    for (i = 0; i < FMV3_SCHEMA_VECTOR_COUNT; i++) {
        const fmv3_schema_vector *v = &fmv3_schema_vectors[i];
        length = encode_schema(v, body);
        check_vector("SCHEMA", &v->m, v, schema_body, FMV3_SCHEMA_DATAGRAM_SIZE - FMV3_HEADER_SIZE, body, length);
        where = "table";
        expect(v->m.body.length >= FMV3_START_INFO_SIZE &&
               same(v->table.layout.data, v->table.layout.length, v->m.body.data + FMV3_START_INFO_SIZE,
                    v->m.body.length - FMV3_START_INFO_SIZE), "layout is the body after the start information");
    }
    for (i = 0; i < FMV3_FRAME_VECTOR_COUNT; i++) {
        const fmv3_frame_vector *v = &fmv3_frame_vectors[i];
        size_t capacity = v->max_udp_size > FMV3_HEADER_SIZE ? v->max_udp_size - FMV3_HEADER_SIZE : 0;
        length = encode_frame(v, body);
        check_vector("FRAME", &v->m, v, frame_body, capacity, body, length);
        /* The table this FRAME is read with, as a receiver stored it from the SCHEMA. */
        where = "table";
        check_layout(&v->table, v->table.layout.data, v->table.layout.length);
        length = encode_layout(&v->table, body, BUFFER_SIZE);
        expect(same(body, length, v->table.layout.data, v->table.layout.length), "encoded table bytes");
    }
    if (differences) {
        printf("%lu difference(s)\n", differences);
        return 1;
    }
    printf("All %d vectors and the %d fixed52 names match.\n", FMV3_HELLO_VECTOR_COUNT + FMV3_CONTROL_VECTOR_COUNT +
           FMV3_SCHEMA_VECTOR_COUNT + FMV3_FRAME_VECTOR_COUNT, FMV3_FIXED52_COUNT);
    return 0;
}
