# ADR 0007: Encrypted backups use the age format

- Status: Accepted
- Date: 2026-11-28
- Deciders: starLORD03

## Context

Every migration saves a `.usmp` backup holding the whole exported calendar: titles, notes,
attendees, meeting links. It sits on the user's disk, often in a downloads folder or a synced
directory, long after the migration. Users need a way to protect it with a passphrase (issue #49).

Requirements:

- No home-made cryptography: only well-reviewed constructions and implementations.
- Open and portable: a user must be able to recover their data without USMP, years later.
- Works on the platforms we ship to (macOS, Linux, Windows) and Python 3.11–3.14.
- The plain bundle format (spec §7) must not change, so existing readers keep working.

## Options

1. **age format through pyrage** (Python binding of `rage`, the Rust age implementation).
   age is a small, documented format (scrypt passphrase recipient, ChaCha20-Poly1305 in 64 KiB
   chunks, HMAC-checked header) with several independent implementations (Go `age`, Rust `rage`,
   others). Prebuilt abi3 wheels for macOS (universal), Linux glibc (x86-64, arm64) and Windows
   x86-64.
2. **Own envelope with `cryptography`** (scrypt + AES-GCM or ChaCha20-Poly1305). The primitives are
   excellent, but the envelope (header, nonces, chunking, key derivation parameters) would be ours
   to design, document, review and keep compatible, and no other tool could open it.
3. **Encrypted ZIP** (ZipCrypto or WinZip AES). ZipCrypto is broken; AES ZIP leaves names and
   sizes readable and needs a further library with patchy support.
4. **OpenPGP** (symmetric). Heavy, many legacy options and pitfalls, awkward Python bindings.

## Decision

Use **age with a passphrase recipient, through pyrage** (option 1). An encrypted bundle is the
complete plain bundle encrypted as one age file. Readers tell the two apart by the first bytes
(`age-encryption.org/v1`), never by file name, and never try to open an encrypted bundle without
a passphrase the user gave (spec §7.1).

- The passphrase is asked twice in a hidden prompt, or read from an environment variable the user
  names (`--passphrase-env`) for scripts. It is never stored, logged, put in a report or in the
  printed repeat command. Passphrases under 8 characters are refused.
- The plain ZIP is built in memory and only the encrypted bytes reach the disk.
- The progress record next to an encrypted backup leaves out loss details, so it reveals nothing
  of the calendar's content.

## Consequences

- Users can always recover a backup with standard tools: `age -d backup.usmp > plain.usmp`.
- pyrage becomes a required dependency. Platforms without a prebuilt wheel (for example Alpine
  Linux or Windows on Arm) need a Rust toolchain to install USMP. If that becomes a problem the
  dependency can move to an optional extra without changing the format.
- An encrypted bundle is decrypted in memory to read it; bundles larger than the reader's size
  limit are refused. Calendar backups are typically a few MB.
- The age scrypt work factor (about half a second per encryption or decryption) is chosen by the
  library; it protects weak passphrases somewhat, but a strong passphrase is still needed.
- Reports are not encrypted: they contain item ids, statuses and loss details, never event titles
  or notes. See the [threat model](../security/threat-model.md).
