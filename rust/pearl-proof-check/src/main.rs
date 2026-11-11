//! An offline caller of the official Pearl PlainProof verifier.
//!
//! The proof implementation is a pinned git dependency, not CPPminer's vendored
//! copy. This executable neither constructs a proof nor implements verification
//! arithmetic. A successful result concerns this reference's PlainProof checks,
//! not a recursive ZK proof, current network activation, or pool acceptance.

use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::{env, fs::File, io::Read, panic, path::PathBuf, process::ExitCode};
use zk_pow::{
    api::{proof::IncompleteBlockHeader, verify::verify_plain_proof},
    ffi::plain_proof::{check_cert_version_eligible, PlainProof},
};

const UPSTREAM_COMMIT: &str = "0dbdb2ffd8b81193a5079bb67a59aecc10ad501c";
const MAX_BASE64_BYTES: u64 = 8 * 1024 * 1024;
const DEFAULT_NBITS: u32 = 0x1e080000;

struct Args {
    header: [u8; 76],
    proof_file: PathBuf,
    cert_version: u32,
    nbits: u32,
}

struct Failure {
    stage: &'static str,
    message: String,
}

impl Failure {
    fn new(stage: &'static str, message: impl ToString) -> Self {
        Self { stage, message: message.to_string() }
    }
}

fn hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

fn parse_header(value: &str) -> Result<[u8; 76], Failure> {
    let bytes = value.as_bytes();
    if bytes.len() != 152 {
        return Err(Failure::new("arguments", "header must contain exactly 152 hexadecimal characters"));
    }
    let mut header = [0u8; 76];
    for (out, pair) in header.iter_mut().zip(bytes.chunks_exact(2)) {
        let high = hex_nibble(pair[0]);
        let low = hex_nibble(pair[1]);
        match (high, low) {
            (Some(high), Some(low)) => *out = (high << 4) | low,
            _ => return Err(Failure::new("arguments", "header contains a non-hexadecimal character")),
        }
    }
    Ok(header)
}

fn parse_args() -> Result<Args, Failure> {
    let mut header = None;
    let mut proof_file = None;
    let mut cert_version = None;
    let mut nbits = None;
    let mut args = env::args_os().skip(1);
    while let Some(name) = args.next() {
        let name = name.to_str().ok_or_else(|| Failure::new("arguments", "argument name is not UTF-8"))?;
        let value = args.next().ok_or_else(|| Failure::new("arguments", "each option requires a value"))?;
        match name {
            "--proof-base64-file" if proof_file.is_none() => proof_file = Some(PathBuf::from(value)),
            "--header-hex" if header.is_none() => {
                let text = value.to_str().ok_or_else(|| Failure::new("arguments", "header is not UTF-8"))?;
                header = Some(parse_header(text)?);
            }
            "--cert-version" if cert_version.is_none() => {
                let version = match value.to_str() {
                    Some("2") => 2,
                    Some("3") => 3,
                    _ => return Err(Failure::new("arguments", "cert-version must be 2 or 3")),
                };
                cert_version = Some(version);
            }
            "--nbits" if nbits.is_none() => {
                let text = value.to_str().ok_or_else(|| Failure::new("arguments", "nbits is not UTF-8"))?;
                let value = if let Some(digits) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
                    u32::from_str_radix(digits, 16)
                } else {
                    text.parse::<u32>()
                }.map_err(|_| Failure::new("arguments", "nbits must be a u32 decimal or 0x-prefixed hexadecimal value"))?;
                nbits = Some(value);
            }
            _ => return Err(Failure::new("arguments", "unknown or duplicate option")),
        }
    }
    Ok(Args {
        header: header.ok_or_else(|| Failure::new("arguments", "missing --header-hex"))?,
        proof_file: proof_file.ok_or_else(|| Failure::new("arguments", "missing --proof-base64-file"))?,
        cert_version: cert_version.ok_or_else(|| Failure::new("arguments", "missing --cert-version"))?,
        nbits: nbits.unwrap_or(DEFAULT_NBITS),
    })
}

fn check(args: &Args) -> Result<usize, Failure> {
    let file = File::open(&args.proof_file).map_err(|error| Failure::new("input_file", error))?;
    let mut encoded = Vec::new();
    file.take(MAX_BASE64_BYTES + 1).read_to_end(&mut encoded)
        .map_err(|error| Failure::new("input_file", error))?;
    if encoded.len() as u64 > MAX_BASE64_BYTES {
        return Err(Failure::new("input_file", "base64 proof file exceeds the 8 MiB bound"));
    }
    let text = std::str::from_utf8(&encoded).map_err(|_| Failure::new("base64", "proof file is not UTF-8"))?;
    let bytes = STANDARD.decode(text.trim()).map_err(|error| Failure::new("base64", error))?;
    let header = IncompleteBlockHeader::from_bytes(&args.header)
        .map_err(|error| Failure::new("header", error))?;
    let proof = PlainProof::deserialize_compat(&bytes)
        .map_err(|error| Failure::new("deserialize", error))?;
    let version = check_cert_version_eligible(args.cert_version, &proof)
        .map_err(|error| Failure::new("certificate", error))?;
    verify_plain_proof(&header, &proof, Some(args.nbits), version.seed_derivation())
        .map_err(|error| Failure::new("verification", error))?;
    Ok(bytes.len())
}

fn base_report() -> Value {
    json!({
        "schema_version": 1,
        "verifier": "official_pearl_verify_plain_proof",
        "upstream_repository": "https://github.com/pearl-research-labs/pearl",
        "upstream_commit": UPSTREAM_COMMIT,
        "proof_kind": "PlainProof",
        "verified": false,
        "zk_proof_generated": false,
        "gpu_executed": false,
        "pool_acceptance_checked": false
    })
}

fn main() -> ExitCode {
    // An upstream panic is a failed check; do not dump an untrusted proof or
    // internal panic payload. The parent runner still needs a process timeout.
    panic::set_hook(Box::new(|_| {}));
    let mut report = base_report();
    let args = match parse_args() {
        Ok(args) => args,
        Err(error) => {
            report["stage"] = json!(error.stage);
            report["error"] = json!(error.message);
            println!("{report}");
            return ExitCode::FAILURE;
        }
    };
    report["cert_version"] = json!(args.cert_version);
    report["nbits"] = json!(format!("0x{:08x}", args.nbits));
    match panic::catch_unwind(|| check(&args)) {
        Ok(Ok(proof_bytes)) => {
            report["stage"] = json!("verified");
            report["verified"] = json!(true);
            report["proof_bytes"] = json!(proof_bytes);
            println!("{report}");
            ExitCode::SUCCESS
        }
        Ok(Err(error)) => {
            report["stage"] = json!(error.stage);
            report["error"] = json!(error.message);
            println!("{report}");
            ExitCode::FAILURE
        }
        Err(_) => {
            report["stage"] = json!("verification_panic");
            report["error"] = json!("the official verifier panicked; proof rejected");
            println!("{report}");
            ExitCode::FAILURE
        }
    }
}
