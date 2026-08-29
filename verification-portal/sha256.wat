(module
  (memory (export "memory") 16)

  ;; ---- Layout ----
  ;; 0x0000 .. 0x0100   : K[0..63] round constants (64 x 4 bytes)
  ;; 0x0200 .. 0x0300   : W[0..63] message schedule scratch (64 x 4 bytes)
  ;; 0x0400 .. 0x0420   : 32-byte digest output
  ;; 0x10000 ..         : input buffer (page 2 onward) -- caller writes raw
  ;;                      bytes here, then calls pad(len) to append the
  ;;                      standard SHA-256 padding in place, then hash(ptr,
  ;;                      paddedLen) to compute the digest.
  (global $INPUT_PTR i32 (i32.const 65536))
  (global $K_PTR i32 (i32.const 0))
  (global $W_PTR i32 (i32.const 512))
  (global $OUT_PTR i32 (i32.const 1024))

  (global $H0 (mut i32) (i32.const 0))
  (global $H1 (mut i32) (i32.const 0))
  (global $H2 (mut i32) (i32.const 0))
  (global $H3 (mut i32) (i32.const 0))
  (global $H4 (mut i32) (i32.const 0))
  (global $H5 (mut i32) (i32.const 0))
  (global $H6 (mut i32) (i32.const 0))
  (global $H7 (mut i32) (i32.const 0))

  (func $rotr (param $x i32) (param $n i32) (result i32)
    (i32.or
      (i32.shr_u (local.get $x) (local.get $n))
      (i32.shl (local.get $x) (i32.sub (i32.const 32) (local.get $n)))))

  (func $init_k
    (i32.store (i32.add (global.get $K_PTR) (i32.const 0)) (i32.const 0x428a2f98))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 4)) (i32.const 0x71374491))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 8)) (i32.const 0xb5c0fbcf))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 12)) (i32.const 0xe9b5dba5))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 16)) (i32.const 0x3956c25b))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 20)) (i32.const 0x59f111f1))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 24)) (i32.const 0x923f82a4))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 28)) (i32.const 0xab1c5ed5))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 32)) (i32.const 0xd807aa98))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 36)) (i32.const 0x12835b01))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 40)) (i32.const 0x243185be))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 44)) (i32.const 0x550c7dc3))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 48)) (i32.const 0x72be5d74))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 52)) (i32.const 0x80deb1fe))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 56)) (i32.const 0x9bdc06a7))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 60)) (i32.const 0xc19bf174))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 64)) (i32.const 0xe49b69c1))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 68)) (i32.const 0xefbe4786))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 72)) (i32.const 0x0fc19dc6))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 76)) (i32.const 0x240ca1cc))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 80)) (i32.const 0x2de92c6f))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 84)) (i32.const 0x4a7484aa))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 88)) (i32.const 0x5cb0a9dc))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 92)) (i32.const 0x76f988da))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 96)) (i32.const 0x983e5152))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 100)) (i32.const 0xa831c66d))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 104)) (i32.const 0xb00327c8))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 108)) (i32.const 0xbf597fc7))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 112)) (i32.const 0xc6e00bf3))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 116)) (i32.const 0xd5a79147))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 120)) (i32.const 0x06ca6351))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 124)) (i32.const 0x14292967))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 128)) (i32.const 0x27b70a85))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 132)) (i32.const 0x2e1b2138))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 136)) (i32.const 0x4d2c6dfc))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 140)) (i32.const 0x53380d13))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 144)) (i32.const 0x650a7354))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 148)) (i32.const 0x766a0abb))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 152)) (i32.const 0x81c2c92e))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 156)) (i32.const 0x92722c85))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 160)) (i32.const 0xa2bfe8a1))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 164)) (i32.const 0xa81a664b))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 168)) (i32.const 0xc24b8b70))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 172)) (i32.const 0xc76c51a3))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 176)) (i32.const 0xd192e819))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 180)) (i32.const 0xd6990624))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 184)) (i32.const 0xf40e3585))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 188)) (i32.const 0x106aa070))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 192)) (i32.const 0x19a4c116))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 196)) (i32.const 0x1e376c08))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 200)) (i32.const 0x2748774c))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 204)) (i32.const 0x34b0bcb5))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 208)) (i32.const 0x391c0cb3))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 212)) (i32.const 0x4ed8aa4a))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 216)) (i32.const 0x5b9cca4f))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 220)) (i32.const 0x682e6ff3))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 224)) (i32.const 0x748f82ee))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 228)) (i32.const 0x78a5636f))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 232)) (i32.const 0x84c87814))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 236)) (i32.const 0x8cc70208))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 240)) (i32.const 0x90befffa))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 244)) (i32.const 0xa4506ceb))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 248)) (i32.const 0xbef9a3f7))
    (i32.store (i32.add (global.get $K_PTR) (i32.const 252)) (i32.const 0xc67178f2))
  )

  ;; Pads the message of `len` bytes already written at $INPUT_PTR, in
  ;; place, per FIPS 180-4 section 5.1.1: append 0x80, then zero bytes,
  ;; then the 8-byte big-endian bit length, so the total length is a
  ;; multiple of 64. Returns the padded total length in bytes. Caller
  ;; must have grown memory so [$INPUT_PTR, $INPUT_PTR + returned_len)
  ;; is valid before calling this.
  (func (export "pad") (param $len i32) (result i32)
    (local $k i32)
    (local $total i32)
    (local $bitlen_hi i32)
    (local $bitlen_lo i32)
    (local $zero_start i32)
    (local $i i32)

    ;; k = smallest non-negative int such that (len + 1 + k) mod 64 == 56,
    ;; i.e. k = ((56 - (len+1) mod 64) + 64) mod 64.
    (local.set $k
      (i32.rem_u
        (i32.add
          (i32.sub (i32.const 56)
            (i32.rem_u (i32.add (local.get $len) (i32.const 1)) (i32.const 64)))
          (i32.const 64))
        (i32.const 64)))

    (i32.store8 (i32.add (global.get $INPUT_PTR) (local.get $len)) (i32.const 0x80))

    (local.set $zero_start (i32.add (global.get $INPUT_PTR) (i32.add (local.get $len) (i32.const 1))))
    (local.set $i (i32.const 0))
    (block $done_zero
      (loop $zero_loop
        (br_if $done_zero (i32.ge_u (local.get $i) (local.get $k)))
        (i32.store8 (i32.add (local.get $zero_start) (local.get $i)) (i32.const 0))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $zero_loop)))

    (local.set $total (i32.add (i32.add (local.get $len) (i32.const 1)) (i32.add (local.get $k) (i32.const 8))))

    ;; 8-byte big-endian bit length = len * 8 (len is well within i32
    ;; range for anything this tool hashes -- audit exports/PDF text are
    ;; nowhere near 2^29 bytes -- so the high 4 bytes are always zero).
    (local.set $bitlen_lo (i32.shl (local.get $len) (i32.const 3)))
    (local.set $bitlen_hi (i32.shr_u (local.get $len) (i32.const 29)))

    (local.set $i (i32.sub (i32.add (global.get $INPUT_PTR) (local.get $total)) (i32.const 8)))
    (i32.store8 (i32.add (local.get $i) (i32.const 0)) (i32.shr_u (local.get $bitlen_hi) (i32.const 24)))
    (i32.store8 (i32.add (local.get $i) (i32.const 1)) (i32.shr_u (local.get $bitlen_hi) (i32.const 16)))
    (i32.store8 (i32.add (local.get $i) (i32.const 2)) (i32.shr_u (local.get $bitlen_hi) (i32.const 8)))
    (i32.store8 (i32.add (local.get $i) (i32.const 3)) (local.get $bitlen_hi))
    (i32.store8 (i32.add (local.get $i) (i32.const 4)) (i32.shr_u (local.get $bitlen_lo) (i32.const 24)))
    (i32.store8 (i32.add (local.get $i) (i32.const 5)) (i32.shr_u (local.get $bitlen_lo) (i32.const 16)))
    (i32.store8 (i32.add (local.get $i) (i32.const 6)) (i32.shr_u (local.get $bitlen_lo) (i32.const 8)))
    (i32.store8 (i32.add (local.get $i) (i32.const 7)) (local.get $bitlen_lo))

    (local.get $total))

  ;; Computes SHA-256 over the `padded_len` bytes at $INPUT_PTR
  ;; (padded_len MUST already be a multiple of 64, i.e. the return value
  ;; of `pad`) and writes the 32-byte big-endian digest to $OUT_PTR.
  (func (export "hash") (param $padded_len i32)
    (local $block i32)
    (local $num_blocks i32)
    (local $b i32)
    (local $t i32)
    (local $w_ptr i32)
    (local $s0 i32) (local $s1 i32)
    (local $a i32) (local $bb i32) (local $c i32) (local $d i32)
    (local $e i32) (local $f i32) (local $g i32) (local $h i32)
    (local $S0 i32) (local $S1 i32) (local $ch i32) (local $maj i32)
    (local $temp1 i32) (local $temp2 i32)
    (local $byte0 i32) (local $byte1 i32) (local $byte2 i32) (local $byte3 i32)

    (call $init_k)
    (global.set $H0 (i32.const 0x6a09e667))
    (global.set $H1 (i32.const 0xbb67ae85))
    (global.set $H2 (i32.const 0x3c6ef372))
    (global.set $H3 (i32.const 0xa54ff53a))
    (global.set $H4 (i32.const 0x510e527f))
    (global.set $H5 (i32.const 0x9b05688c))
    (global.set $H6 (i32.const 0x1f83d9ab))
    (global.set $H7 (i32.const 0x5be0cd19))

    (local.set $num_blocks (i32.div_u (local.get $padded_len) (i32.const 64)))
    (local.set $b (i32.const 0))

    (block $blocks_done
      (loop $block_loop
        (br_if $blocks_done (i32.ge_u (local.get $b) (local.get $num_blocks)))
        (local.set $block (i32.add (global.get $INPUT_PTR) (i32.mul (local.get $b) (i32.const 64))))

        ;; W[0..15] = big-endian 32-bit words directly from the block.
        (local.set $t (i32.const 0))
        (block $w0_done
          (loop $w0_loop
            (br_if $w0_done (i32.ge_u (local.get $t) (i32.const 16)))
            (local.set $byte0 (i32.load8_u (i32.add (local.get $block) (i32.mul (local.get $t) (i32.const 4)))))
            (local.set $byte1 (i32.load8_u (i32.add (i32.add (local.get $block) (i32.mul (local.get $t) (i32.const 4))) (i32.const 1))))
            (local.set $byte2 (i32.load8_u (i32.add (i32.add (local.get $block) (i32.mul (local.get $t) (i32.const 4))) (i32.const 2))))
            (local.set $byte3 (i32.load8_u (i32.add (i32.add (local.get $block) (i32.mul (local.get $t) (i32.const 4))) (i32.const 3))))
            (i32.store
              (i32.add (global.get $W_PTR) (i32.mul (local.get $t) (i32.const 4)))
              (i32.or
                (i32.or (i32.shl (local.get $byte0) (i32.const 24)) (i32.shl (local.get $byte1) (i32.const 16)))
                (i32.or (i32.shl (local.get $byte2) (i32.const 8)) (local.get $byte3))))
            (local.set $t (i32.add (local.get $t) (i32.const 1)))
            (br $w0_loop)))

        ;; W[16..63]
        (local.set $t (i32.const 16))
        (block $w1_done
          (loop $w1_loop
            (br_if $w1_done (i32.ge_u (local.get $t) (i32.const 64)))
            (local.set $w_ptr (i32.add (global.get $W_PTR) (i32.mul (local.get $t) (i32.const 4))))

            ;; s0 = rotr(W[t-15],7) xor rotr(W[t-15],18) xor shr(W[t-15],3)
            (local.set $s0
              (i32.load (i32.add (global.get $W_PTR) (i32.mul (i32.sub (local.get $t) (i32.const 15)) (i32.const 4)))))
            (local.set $s0
              (i32.xor
                (i32.xor (call $rotr (local.get $s0) (i32.const 7)) (call $rotr (local.get $s0) (i32.const 18)))
                (i32.shr_u (local.get $s0) (i32.const 3))))

            ;; s1 = rotr(W[t-2],17) xor rotr(W[t-2],19) xor shr(W[t-2],10)
            (local.set $s1
              (i32.load (i32.add (global.get $W_PTR) (i32.mul (i32.sub (local.get $t) (i32.const 2)) (i32.const 4)))))
            (local.set $s1
              (i32.xor
                (i32.xor (call $rotr (local.get $s1) (i32.const 17)) (call $rotr (local.get $s1) (i32.const 19)))
                (i32.shr_u (local.get $s1) (i32.const 10))))

            (i32.store (local.get $w_ptr)
              (i32.add
                (i32.add (local.get $s0) (i32.load (i32.add (global.get $W_PTR) (i32.mul (i32.sub (local.get $t) (i32.const 16)) (i32.const 4)))))
                (i32.add (local.get $s1) (i32.load (i32.add (global.get $W_PTR) (i32.mul (i32.sub (local.get $t) (i32.const 7)) (i32.const 4)))))))

            (local.set $t (i32.add (local.get $t) (i32.const 1)))
            (br $w1_loop)))

        (local.set $a (global.get $H0))
        (local.set $bb (global.get $H1))
        (local.set $c (global.get $H2))
        (local.set $d (global.get $H3))
        (local.set $e (global.get $H4))
        (local.set $f (global.get $H5))
        (local.set $g (global.get $H6))
        (local.set $h (global.get $H7))

        (local.set $t (i32.const 0))
        (block $rounds_done
          (loop $rounds_loop
            (br_if $rounds_done (i32.ge_u (local.get $t) (i32.const 64)))

            (local.set $S1
              (i32.xor
                (i32.xor (call $rotr (local.get $e) (i32.const 6)) (call $rotr (local.get $e) (i32.const 11)))
                (call $rotr (local.get $e) (i32.const 25))))
            (local.set $ch
              (i32.xor (i32.and (local.get $e) (local.get $f)) (i32.and (i32.xor (local.get $e) (i32.const -1)) (local.get $g))))
            (local.set $temp1
              (i32.add
                (i32.add (local.get $h) (local.get $S1))
                (i32.add (local.get $ch)
                  (i32.add
                    (i32.load (i32.add (global.get $K_PTR) (i32.mul (local.get $t) (i32.const 4))))
                    (i32.load (i32.add (global.get $W_PTR) (i32.mul (local.get $t) (i32.const 4))))))))
            (local.set $S0
              (i32.xor
                (i32.xor (call $rotr (local.get $a) (i32.const 2)) (call $rotr (local.get $a) (i32.const 13)))
                (call $rotr (local.get $a) (i32.const 22))))
            (local.set $maj
              (i32.xor
                (i32.xor (i32.and (local.get $a) (local.get $bb)) (i32.and (local.get $a) (local.get $c)))
                (i32.and (local.get $bb) (local.get $c))))
            (local.set $temp2 (i32.add (local.get $S0) (local.get $maj)))

            (local.set $h (local.get $g))
            (local.set $g (local.get $f))
            (local.set $f (local.get $e))
            (local.set $e (i32.add (local.get $d) (local.get $temp1)))
            (local.set $d (local.get $c))
            (local.set $c (local.get $bb))
            (local.set $bb (local.get $a))
            (local.set $a (i32.add (local.get $temp1) (local.get $temp2)))

            (local.set $t (i32.add (local.get $t) (i32.const 1)))
            (br $rounds_loop)))

        (global.set $H0 (i32.add (global.get $H0) (local.get $a)))
        (global.set $H1 (i32.add (global.get $H1) (local.get $bb)))
        (global.set $H2 (i32.add (global.get $H2) (local.get $c)))
        (global.set $H3 (i32.add (global.get $H3) (local.get $d)))
        (global.set $H4 (i32.add (global.get $H4) (local.get $e)))
        (global.set $H5 (i32.add (global.get $H5) (local.get $f)))
        (global.set $H6 (i32.add (global.get $H6) (local.get $g)))
        (global.set $H7 (i32.add (global.get $H7) (local.get $h)))

        (local.set $b (i32.add (local.get $b) (i32.const 1)))
        (br $block_loop)))

    ;; Write H0..H7 big-endian to $OUT_PTR.
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 0)) (global.get $H0))
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 4)) (global.get $H1))
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 8)) (global.get $H2))
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 12)) (global.get $H3))
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 16)) (global.get $H4))
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 20)) (global.get $H5))
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 24)) (global.get $H6))
    (call $store_be32 (i32.add (global.get $OUT_PTR) (i32.const 28)) (global.get $H7))
  )

  (func $store_be32 (param $ptr i32) (param $v i32)
    (i32.store8 (i32.add (local.get $ptr) (i32.const 0)) (i32.shr_u (local.get $v) (i32.const 24)))
    (i32.store8 (i32.add (local.get $ptr) (i32.const 1)) (i32.shr_u (local.get $v) (i32.const 16)))
    (i32.store8 (i32.add (local.get $ptr) (i32.const 2)) (i32.shr_u (local.get $v) (i32.const 8)))
    (i32.store8 (i32.add (local.get $ptr) (i32.const 3)) (local.get $v)))

  (export "INPUT_PTR" (global $INPUT_PTR))
  (export "OUT_PTR" (global $OUT_PTR))
)
