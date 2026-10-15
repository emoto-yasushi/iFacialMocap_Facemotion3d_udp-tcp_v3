/* FMV3 contract revision 6: the answer key (expected bytes) in C99.
 *
 * Optional material for developing an FMV3 sender or receiver. It needs only a C99
 * compiler and the C standard library.
 * Build and run the check (exit status 0: every vector matches):
 *
 *     cc -std=c99 -o golden_vectors_check golden_vectors.c golden_vectors_check.c
 *     ./golden_vectors_check
 *
 * To check your own code, compile golden_vectors.c with it:
 *   - encoder: build each message from the expected fields and memcmp it with `message`
 *     (the whole message in one piece, as sent over TCP; SCHEMA_ACK is sent only over
 *     UDP) and with udp[0] ... udp[udp_count - 1] (the UDP datagrams, in order);
 *   - decoder: decode `message`, or the datagrams in order, and compare what you read
 *     with the expected fields.
 * golden_vectors_check.c does both for every vector.
 *
 * Every number on the wire is little-endian. Read and write it byte by byte; do not cast
 * the bytes to a C struct (padding, alignment and byte order differ between targets).
 * Names are UTF-8 bytes with an explicit length, and the head and eye values are given
 * as Float32 bit patterns, so no comparison depends on the source character set or on
 * floating-point rounding.
 *
 * PROTOCOL_V3.md is the definition; its Appendix A cites these vectors by name.
 * Generated file: do not edit.
 */
#ifndef FMV3_GOLDEN_VECTORS_H
#define FMV3_GOLDEN_VECTORS_H

#include <stddef.h>
#include <stdint.h>

#define FMV3_CONTRACT_REVISION 6

/* Header, 40 bytes: magic "FMV3" (46 4d 56 33), message_type u8, flags u8,
 * header_size u16, session_id u64, schema_id u32, sequence u32, source_token u32,
 * total_payload_length u32, part_index u16, part_count u16, chunk_length u32. */
#define FMV3_HEADER_SIZE 40
/* HELLO body: requested_fps u16, max_udp_size u16, contract_revision u32, requested_mode u16. */
#define FMV3_HELLO_BODY_SIZE 10
/* SCHEMA start information: client_nonce u64, actual_fps u16, max_udp_size u16,
 * lease_ms u32, contract_revision u32. */
#define FMV3_START_INFO_SIZE 20
/* SCHEMA descriptor, after the start information: mode u16, blend_encoding u8,
 * schema_format_version u8, blend_count u16. Variable mode then has app and profile
 * (u16 length + UTF-8) and blend_count names (u8 length + UTF-8). */
#define FMV3_DESCRIPTOR_SIZE 6
/* FRAME body: blend_count values (i16 or i32), then 12 Float32: head rx ry rz px py pz,
 * right eye rx ry rz, left eye rx ry rz. */
#define FMV3_POSE_SIZE 48
/* The largest UDP SCHEMA datagram, header included: every fragment except the last has
 * exactly this size; the last fragment, or a SCHEMA that is not split, can be shorter
 * (PROTOCOL_V3.md 3.9). A FRAME datagram is at most max_udp_size. */
#define FMV3_SCHEMA_DATAGRAM_SIZE 576

/* message_type */
#define FMV3_HELLO 1
#define FMV3_SCHEMA 2
#define FMV3_SCHEMA_ACK 3
#define FMV3_FRAME 4
#define FMV3_GET_SCHEMA 5
#define FMV3_PING 6
#define FMV3_PONG 7
#define FMV3_STOP 8
#define FMV3_ERROR 9

#define FMV3_FLAG_TRACKING 1  /* FRAME flags bit 0 */
#define FMV3_FLAG_PLAYBACK 2  /* FRAME flags bit 1 */
#define FMV3_MODE_VARIABLE 0
#define FMV3_MODE_FIXED52 1
#define FMV3_ENCODING_I16 1
#define FMV3_ENCODING_I32 2

/* A byte array and its length. */
typedef struct {
    const uint8_t *data;
    size_t length;
} fmv3_bytes;

/* What every vector has: the expected header fields and the expected bytes. */
typedef struct {
    const char *name;           /* vector name */
    uint8_t message_type;
    uint8_t flags;
    uint64_t session_id;
    uint32_t schema_id;
    uint32_t sequence;
    uint32_t source_token;
    fmv3_bytes body;            /* the whole body; its length is total_payload_length */
    fmv3_bytes message;         /* the complete message: one header, then the whole body */
    const fmv3_bytes *udp;      /* the UDP datagrams in order: udp[i] has part_index i.
                                   A message that is not split is one datagram, equal to message */
    size_t udp_count;           /* part_count */
} fmv3_message_vector;

/* HELLO (PC to iOS). */
typedef struct {
    fmv3_message_vector m;
    uint16_t requested_fps;
    uint16_t max_udp_size;
    uint32_t contract_revision;
    uint16_t requested_mode;
} fmv3_hello_vector;

/* SCHEMA_ACK, GET_SCHEMA, PING, PONG and STOP (no body), ERROR (UTF-8 text). */
typedef struct {
    fmv3_message_vector m;
    fmv3_bytes text;            /* the ERROR text; length 0 for the others */
} fmv3_control_vector;

/* The table that a SCHEMA announces and that FRAMEs are read with. */
typedef struct {
    uint16_t mode;
    uint8_t blend_encoding;
    uint8_t schema_format_version;
    uint16_t blend_count;
    fmv3_bytes app;             /* UTF-8 without its length; length 0 in fixed52 */
    fmv3_bytes profile;         /* UTF-8 without its length; length 0 in fixed52 */
    const fmv3_bytes *names;    /* blend_count names, UTF-8 without the length byte. Fixed52 sends
                                   no names; the receiver uses fmv3_fixed52_names */
    fmv3_bytes layout;          /* the table as sent: the SCHEMA body after the start information */
} fmv3_table;

/* SCHEMA (iOS to PC): the start information, then the table. */
typedef struct {
    fmv3_message_vector m;
    uint64_t client_nonce;      /* the session_id of the HELLO; 0 for a manual start */
    uint16_t actual_fps;
    uint16_t max_udp_size;
    uint32_t lease_ms;
    uint32_t contract_revision;
    fmv3_table table;
} fmv3_schema_vector;

/* FRAME (iOS to PC). */
typedef struct {
    fmv3_message_vector m;
    uint16_t max_udp_size;      /* of the session: the largest FRAME datagram */
    fmv3_table table;           /* the table this FRAME is read with */
    const int32_t *values;      /* blend_count values in integer percent (-25 means -0.25) */
    uint32_t pose_bits[12];     /* the 12 Float32 values as bit patterns, in FRAME order */
} fmv3_frame_vector;

#define FMV3_FIXED52_COUNT 52
extern const fmv3_bytes fmv3_fixed52_names[FMV3_FIXED52_COUNT];

#define FMV3_HELLO_VECTOR_COUNT 2
/* fmv3_hello_vectors: hello, hello_fixed52 */
extern const fmv3_hello_vector fmv3_hello_vectors[FMV3_HELLO_VECTOR_COUNT];

#define FMV3_CONTROL_VECTOR_COUNT 6
/* fmv3_control_vectors: schema_ack, get_schema_latest, ping_sequence_7, pong_sequence_7, stop, error_busy */
extern const fmv3_control_vector fmv3_control_vectors[FMV3_CONTROL_VECTOR_COUNT];

#define FMV3_SCHEMA_VECTOR_COUNT 7
/* fmv3_schema_vectors: initial_ifacialmocap, initial_facemotion3d, changed_i32, fragmented_initial, manual_ifacialmocap, manual_facemotion3d_fragmented, fixed52_manual */
extern const fmv3_schema_vector fmv3_schema_vectors[FMV3_SCHEMA_VECTOR_COUNT];

#define FMV3_FRAME_VECTOR_COUNT 4
/* fmv3_frame_vectors: i16_manual, i32_custom_unicode, fragmented_frame, fixed52_i32 */
extern const fmv3_frame_vector fmv3_frame_vectors[FMV3_FRAME_VECTOR_COUNT];

#endif
