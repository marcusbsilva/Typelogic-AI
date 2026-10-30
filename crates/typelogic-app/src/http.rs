//! Minimal HTTP/1.1 for the loopback interface only: a threaded server for the UI/API and a client for llama-server.
//!
//! Scope is deliberately small: one request per connection (`Connection: close`), `Content-Length` request bodies,
//! `Content-Length` or chunked response bodies. Requests whose Host header is not the loopback address are refused
//! (DNS-rebinding protection), and so are cross-origin browser POSTs.
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::Arc;
use std::time::Duration;

pub const MAX_HEADER_BYTES: usize = 64 * 1024;
pub const MAX_BODY_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }

    /// Raw value of `name` in the query string (no percent-decoding: simple identifiers).
    pub fn query_param(&self, name: &str) -> Option<&str> {
        self.query.split('&').filter_map(|kv| kv.split_once('=')).find(|(k, _)| *k == name).map(|(_, v)| v)
    }

    /// Percent-decoded value of `name` (`+` is a space, as in form encoding).
    pub fn query_text(&self, name: &str) -> Option<String> {
        let raw = self.query_param(name)?;
        let b = raw.as_bytes();
        let mut out = Vec::with_capacity(b.len());
        let mut i = 0;
        while i < b.len() {
            match b[i] {
                b'%' if i + 2 < b.len() => {
                    match u8::from_str_radix(std::str::from_utf8(&b[i + 1..i + 3]).unwrap_or("zz"), 16) {
                        Ok(v) => {
                            out.push(v);
                            i += 3;
                            continue;
                        }
                        Err(_) => out.push(b'%'),
                    }
                }
                b'+' => out.push(b' '),
                c => out.push(c),
            }
            i += 1;
        }
        Some(String::from_utf8_lossy(&out).into_owned())
    }
}

#[derive(Debug, Clone)]
pub struct Response {
    pub status: u16,
    pub content_type: &'static str,
    pub body: Vec<u8>,
}

impl Response {
    pub fn json(status: u16, body: impl Into<Vec<u8>>) -> Self { Self { status, content_type: "application/json; charset=utf-8", body: body.into() } }
    pub fn html(body: impl Into<Vec<u8>>) -> Self { Self { status: 200, content_type: "text/html; charset=utf-8", body: body.into() } }
    pub fn error(status: u16, message: &str) -> Self {
        let mut text = String::from("{\"error\": ");
        typelogic_core::json::write_string(&mut text, message, false);
        text.push('}');
        Self::json(status, text)
    }
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        401 => "Unauthorized",
        422 => "Unprocessable Entity",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        413 => "Payload Too Large",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        _ => "Status",
    }
}

fn invalid(message: &str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message.to_string()) }

/// Read the request line and headers (at most MAX_HEADER_BYTES) -> (start line, headers).
fn read_head(reader: &mut impl BufRead) -> io::Result<(String, Vec<(String, String)>)> {
    let mut total = 0usize;
    let mut lines = Vec::new();
    loop {
        let mut line = String::new();
        let n = reader.read_line(&mut line)?;
        if n == 0 {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "connection closed in the header"));
        }
        total += n;
        if total > MAX_HEADER_BYTES {
            return Err(invalid("header too large"));
        }
        let line = line.trim_end_matches(['\r', '\n']).to_string();
        if line.is_empty() {
            break;
        }
        lines.push(line);
    }
    let mut iter = lines.into_iter();
    let start = iter.next().ok_or_else(|| invalid("empty request"))?;
    let headers = iter
        .filter_map(|l| l.split_once(':').map(|(k, v)| (k.trim().to_string(), v.trim().to_string())))
        .collect();
    Ok((start, headers))
}

fn content_length(headers: &[(String, String)]) -> io::Result<Option<usize>> {
    match headers.iter().find(|(k, _)| k.eq_ignore_ascii_case("content-length")) {
        None => Ok(None),
        Some((_, v)) => v.parse::<usize>().map(Some).map_err(|_| invalid("bad Content-Length")),
    }
}

pub fn parse_request(stream: impl Read) -> io::Result<Request> {
    let mut reader = BufReader::new(stream);
    let (start, headers) = read_head(&mut reader)?;
    let mut parts = start.split(' ');
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().ok_or_else(|| invalid("bad request line"))?;
    if !parts.next().is_some_and(|v| v.starts_with("HTTP/1.")) {
        return Err(invalid("bad request line"));
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("transfer-encoding")) {
        return Err(invalid("chunked request bodies are not accepted"));
    }
    let length = content_length(&headers)?.unwrap_or(0);
    if length > MAX_BODY_BYTES {
        return Err(io::Error::new(io::ErrorKind::OutOfMemory, "body too large"));
    }
    let mut body = vec![0u8; length];
    reader.read_exact(&mut body)?;
    Ok(Request { method, path: path.to_string(), query: query.to_string(), headers, body })
}

/// The page may only load and call itself (as in 1.x).
const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; \
                   frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

pub fn write_response(stream: &mut impl Write, response: &Response) -> io::Result<()> {
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\
         Referrer-Policy: no-referrer\r\nContent-Security-Policy: {CSP}\r\nConnection: close\r\n\r\n",
        response.status,
        reason(response.status),
        response.content_type,
        response.body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(&response.body)?;
    stream.flush()
}

/// Host header must name the loopback address with our port; a browser POST must come from our own origin.
pub fn local_request_allowed(request: &Request, port: u16) -> bool {
    let allowed_hosts = [format!("127.0.0.1:{port}"), format!("localhost:{port}"), format!("[::1]:{port}")];
    let host_ok = request.header("host").is_some_and(|h| allowed_hosts.iter().any(|a| a.eq_ignore_ascii_case(h)));
    let origin_ok = match request.header("origin") {
        None => true,
        Some(origin) => allowed_hosts.iter().any(|a| origin.eq_ignore_ascii_case(&format!("http://{a}"))),
    };
    host_ok && origin_ok
}

pub type Handler = dyn Fn(Request) -> Response + Send + Sync;

/// Serve forever, one thread per connection.
pub fn serve(listener: TcpListener, handler: Arc<Handler>) -> io::Result<()> {
    let port = listener.local_addr()?.port();
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        let handler = Arc::clone(&handler);
        std::thread::spawn(move || {
            let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
            let response = match parse_request(&mut stream) {
                Ok(request) if !local_request_allowed(&request, port) => Response::error(403, "Origin or Host not allowed"),
                Ok(request) => handler(request),
                Err(e) if e.kind() == io::ErrorKind::OutOfMemory => Response::error(413, "request body too large"),
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return,
                Err(e) => Response::error(400, &e.to_string()),
            };
            let _ = write_response(&mut stream, &response);
        });
    }
    Ok(())
}

// ------------------------------------------------------------------------------------------------ client

fn decode_chunked(mut data: &[u8]) -> io::Result<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let end = data.windows(2).position(|w| w == b"\r\n").ok_or_else(|| invalid("bad chunk"))?;
        let size_text = std::str::from_utf8(&data[..end]).map_err(|_| invalid("bad chunk"))?;
        let size = usize::from_str_radix(size_text.split(';').next().unwrap_or("").trim(), 16).map_err(|_| invalid("bad chunk size"))?;
        data = &data[end + 2..];
        if size == 0 {
            return Ok(out);
        }
        if data.len() < size + 2 {
            return Err(invalid("truncated chunk"));
        }
        out.extend_from_slice(&data[..size]);
        data = &data[size + 2..];
    }
}

/// One request to a loopback HTTP server -> (status, body).
pub fn call(addr: SocketAddr, method: &str, path: &str, body: Option<&[u8]>, timeout: Duration) -> io::Result<(u16, Vec<u8>)> {
    if !addr.ip().is_loopback() {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "the client only talks to the loopback address"));
    }
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(Duration::from_secs(30)))?;
    let body = body.unwrap_or(&[]);
    let head = format!(
        "{method} {path} HTTP/1.1\r\nHost: {addr}\r\nAccept: application/json\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)?;
    let mut reader = BufReader::new(stream);
    let (start, headers) = read_head(&mut reader)?;
    let status = start.split(' ').nth(1).and_then(|s| s.parse::<u16>().ok()).ok_or_else(|| invalid("bad status line"))?;
    let chunked = headers.iter().any(|(k, v)| k.eq_ignore_ascii_case("transfer-encoding") && v.to_ascii_lowercase().contains("chunked"));
    let mut data = Vec::new();
    match content_length(&headers)? {
        Some(n) if !chunked => {
            data.resize(n, 0);
            reader.read_exact(&mut data)?;
        }
        _ => {
            reader.take(MAX_BODY_BYTES as u64 * 4).read_to_end(&mut data)?;
            if chunked {
                data = decode_chunked(&data)?;
            }
        }
    }
    Ok((status, data))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_post() {
        let raw = b"POST /v1/systemone?engine=specialist HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nContent-Length: 2\r\n\r\n{}";
        let r = parse_request(&raw[..]).unwrap();
        assert_eq!((r.method.as_str(), r.path.as_str(), r.body.as_slice()), ("POST", "/v1/systemone", &b"{}"[..]));
        assert_eq!(r.query_param("engine"), Some("specialist"));
        assert!(local_request_allowed(&r, 8765));
        assert!(!local_request_allowed(&r, 9000));
    }

    #[test]
    fn refuses_foreign_hosts_and_origins() {
        let raw = b"GET / HTTP/1.1\r\nHost: evil.example:8765\r\n\r\n";
        assert!(!local_request_allowed(&parse_request(&raw[..]).unwrap(), 8765));
        let raw = b"POST /x HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nOrigin: http://evil.example\r\nContent-Length: 0\r\n\r\n";
        assert!(!local_request_allowed(&parse_request(&raw[..]).unwrap(), 8765));
    }

    #[test]
    fn rejects_oversized_and_chunked_bodies() {
        let raw = format!("POST / HTTP/1.1\r\nHost: 127.0.0.1:1\r\nContent-Length: {}\r\n\r\n", MAX_BODY_BYTES + 1);
        assert_eq!(parse_request(raw.as_bytes()).unwrap_err().kind(), io::ErrorKind::OutOfMemory);
        let raw = b"POST / HTTP/1.1\r\nHost: 127.0.0.1:1\r\nTransfer-Encoding: chunked\r\n\r\n";
        assert!(parse_request(&raw[..]).is_err());
    }

    #[test]
    fn decodes_chunks() {
        assert_eq!(decode_chunked(b"4\r\nWiki\r\n5;x=y\r\npedia\r\n0\r\n\r\n").unwrap(), b"Wikipedia");
        assert!(decode_chunked(b"9\r\nabc\r\n").is_err());
    }

    #[test]
    fn round_trip_over_loopback() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handler: Arc<Handler> = Arc::new(|r: Request| Response::json(200, format!("{{\"echo\": {}}}", r.body.len())));
        std::thread::spawn(move || serve(listener, handler));
        let (status, body) = call(addr, "POST", "/", Some(b"abc"), Duration::from_secs(5)).unwrap();
        assert_eq!((status, body.as_slice()), (200, &b"{\"echo\": 3}"[..]));
    }
}
