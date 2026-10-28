//! `typelogic._core`: PyO3 bindings of `typelogic-core`, built with maturin.
//! Python objects are converted structurally (dict order preserved, big ints kept exact). Inputs the core
//! cannot represent raise `CoreUnsupported`, and `typelogic/core.py` then uses the Python reference.
use pyo3::exceptions::{PyOverflowError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use typelogic_core::json::{normalize_int, Map, Value, MAX_DEPTH};
use typelogic_core::{api, exports, readout, router, schema, CoreError, ErrorKind};

pyo3::create_exception!(_core, CoreUnsupported, pyo3::exceptions::PyException, "Input outside the Rust core's exact domain; use the Python reference.");

fn err(e: CoreError) -> PyErr {
    match e.kind {
        ErrorKind::Value => PyValueError::new_err(e.message),
        ErrorKind::Runtime => PyRuntimeError::new_err(e.message),
        ErrorKind::Overflow => PyOverflowError::new_err(e.message),
        ErrorKind::Type => PyTypeError::new_err(e.message),
        ErrorKind::Unsupported => CoreUnsupported::new_err(e.message),
    }
}

fn unsupported(msg: &str) -> PyErr { CoreUnsupported::new_err(msg.to_owned()) }

fn to_value(obj: &Bound<'_, PyAny>, depth: usize) -> PyResult<Value> {
    if depth > MAX_DEPTH {
        return Err(unsupported("nesting too deep"));
    }
    if obj.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(b) = obj.cast::<PyBool>() {
        return Ok(Value::Bool(b.is_true()));
    }
    if obj.is_instance_of::<PyInt>() {
        if let Ok(i) = obj.extract::<i64>() {
            return Ok(Value::Int(i.to_string()));
        }
        let text = obj.str()?;
        return Ok(Value::Int(normalize_int(&text.to_cow()?)));
    }
    if obj.is_instance_of::<PyFloat>() {
        return Ok(Value::Float(obj.extract::<f64>()?));
    }
    if let Ok(s) = obj.cast::<PyString>() {
        return match s.to_cow() {
            Ok(text) => Ok(Value::Str(text.into_owned())),
            Err(_) => Err(unsupported("string is not valid Unicode text (lone surrogate)")),
        };
    }
    if let Ok(list) = obj.cast::<PyList>() {
        let mut out = Vec::with_capacity(list.len());
        for item in list.iter() {
            out.push(to_value(&item, depth + 1)?);
        }
        return Ok(Value::Array(out));
    }
    if let Ok(t) = obj.cast::<PyTuple>() {
        let mut out = Vec::with_capacity(t.len());
        for item in t.iter() {
            out.push(to_value(&item, depth + 1)?);
        }
        return Ok(Value::Array(out));
    }
    if let Ok(d) = obj.cast::<PyDict>() {
        let mut m = Map::with_capacity(d.len());
        for (k, v) in d.iter() {
            let key = k.cast::<PyString>().map_err(|_| unsupported("non-string dict key"))?;
            let key = key.to_cow().map_err(|_| unsupported("dict key is not valid Unicode"))?.into_owned();
            m.push_unique(key, to_value(&v, depth + 1)?);
        }
        return Ok(Value::Object(m));
    }
    Err(unsupported("unsupported Python type"))
}

fn to_py<'py>(py: Python<'py>, v: &Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match v {
        Value::Null => py.None().into_bound(py),
        Value::Bool(b) => PyBool::new(py, *b).to_owned().into_any(),
        Value::Int(s) => match s.parse::<i64>() {
            Ok(i) => i.into_pyobject(py)?.into_any(),
            Err(_) => py.get_type::<PyInt>().call1((s.as_str(),))?,
        },
        Value::Float(f) => PyFloat::new(py, *f).into_any(),
        Value::Str(s) => PyString::new(py, s).into_any(),
        Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(to_py(py, item)?)?;
            }
            list.into_any()
        }
        Value::Object(m) => {
            let d = PyDict::new(py);
            for (k, item) in m.iter() {
                d.set_item(k, to_py(py, item)?)?;
            }
            d.into_any()
        }
    })
}

fn map_arg(obj: &Bound<'_, PyAny>, what: &str) -> PyResult<Map> {
    match to_value(obj, 0)? {
        Value::Object(m) => Ok(m),
        _ => Err(unsupported(&format!("{what} must be a dict"))),
    }
}

/// Core version and the Unicode version of its character tables.
#[pyfunction]
fn version() -> (&'static str, &'static str) { (typelogic_core::VERSION, typelogic_core::UNICODE_VERSION) }

/// `schema.validate(payload)` -> `(state, questions, threshold)`.
#[pyfunction]
fn validate<'py>(py: Python<'py>, payload: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    let v = schema::validate(&to_value(payload, 0)?).map_err(err)?;
    let questions = PyList::empty(py);
    for q in &v.questions {
        questions.append(to_py(py, &q.to_value())?)?;
    }
    PyTuple::new(py, [PyString::new(py, &v.state).into_any(), questions.into_any(), PyFloat::new(py, v.threshold).into_any()])
}

/// `schema.answers(questions, rows, threshold, calibration)`.
#[pyfunction]
fn answers<'py>(py: Python<'py>, questions: &Bound<'py, PyAny>, rows: &Bound<'py, PyAny>, threshold: f64, calibration: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let qs = match to_value(questions, 0)? {
        Value::Array(items) => items.iter().map(schema::Question::from_value).collect::<Result<Vec<_>, _>>().map_err(err)?,
        _ => return Err(unsupported("questions must be a list")),
    };
    let mut parsed = Vec::new();
    for row in rows.try_iter()? {
        let mut values = Vec::new();
        for v in row?.try_iter()? {
            values.push(v?.extract::<f64>().map_err(|_| unsupported("row values must be real numbers"))?);
        }
        parsed.push(values);
    }
    let out = schema::answers(&qs, &parsed, threshold, &to_value(calibration, 0)?).map_err(err)?;
    to_py(py, &Value::Object(out))
}

/// `schema.canonical(value)`; `compact=True` gives the fingerprint form.
#[pyfunction]
#[pyo3(signature = (value, compact=false))]
fn canonical(value: &Bound<'_, PyAny>, compact: bool) -> PyResult<String> {
    let opts = if compact { typelogic_core::json::DumpOptions::COMPACT_SORTED } else { typelogic_core::json::DumpOptions::CANONICAL };
    typelogic_core::json::dumps(&to_value(value, 0)?, opts).map_err(err)
}

#[pyfunction]
fn canonical_bytes<'py>(py: Python<'py>, record: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    Ok(PyBytes::new(py, exports::canonical_bytes(&map_arg(record, "record")?).map_err(err)?.as_bytes()))
}

#[pyfunction]
fn digests<'py>(py: Python<'py>, record: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let (sha512, md5) = exports::digests(&map_arg(record, "record")?).map_err(err)?;
    let d = PyDict::new(py);
    d.set_item("sha512", sha512)?;
    d.set_item("md5", md5)?;
    Ok(d)
}

#[pyfunction]
fn verification<'py>(py: Python<'py>, record: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    to_py(py, &Value::Object(exports::verification(&map_arg(record, "record")?).map_err(err)?))
}

#[pyfunction]
fn lite<'py>(py: Python<'py>, record: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    to_py(py, &Value::Object(exports::lite(&map_arg(record, "record")?).map_err(err)?))
}

#[pyfunction]
#[pyo3(signature = (decision_id, format=None))]
fn export_filename(decision_id: &Bound<'_, PyAny>, format: Option<&Bound<'_, PyAny>>) -> PyResult<String> {
    let fmt = match format { None => Ok("full".to_owned()), Some(f) => f.extract::<String>() };
    let (Ok(id), Ok(fmt)) = (decision_id.extract::<String>(), fmt) else {
        return Err(PyValueError::new_err("Invalid decision ID or format"));
    };
    exports::export_filename(&id, &fmt).map_err(err)
}

#[pyfunction]
fn route<'py>(py: Python<'py>, request: &Bound<'py, PyAny>, profiles: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let profiles = router::parse_profiles(&to_value(profiles, 0)?).map_err(err)?;
    to_py(py, &Value::Object(router::route(&to_value(request, 0)?, &profiles).map_err(err)?))
}

/// Generic JSON contract shared with Node.js, Lua and the CLI.
#[pyfunction]
fn call(method: &str, params_json: &str) -> String { api::call(method, params_json) }

fn f32s(data: &[u8], what: &str) -> PyResult<Vec<f32>> {
    if data.len() % 4 != 0 {
        return Err(PyValueError::new_err(format!("{what}: float32 buffer length must be a multiple of 4")));
    }
    Ok(data.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect())
}

/// Rust pointer readout. Weights are little-endian float32 bytes (`numpy.ndarray.tobytes()`).
#[pyclass(frozen)]
struct PointerHead {
    inner: readout::PointerHead,
}

#[pymethods]
impl PointerHead {
    #[new]
    #[pyo3(signature = (d, dp, qw, qb, kw, kb, temperature=1.0))]
    fn new(d: usize, dp: usize, qw: &[u8], qb: &[u8], kw: &[u8], kb: &[u8], temperature: f64) -> PyResult<Self> {
        let inner = readout::PointerHead::new(d, dp, f32s(qw, "qw")?, f32s(qb, "qb")?, f32s(kw, "kw")?, f32s(kb, "kb")?, temperature).map_err(err)?;
        Ok(Self { inner })
    }
    #[getter]
    fn temperature(&self) -> f64 { self.inner.temperature }
    #[getter]
    fn hidden_size(&self) -> usize { self.inner.d }
    /// Logits for one question (`decision`: d float32, `options`: K×d float32).
    fn logits(&self, py: Python<'_>, decision: &[u8], options: &[u8]) -> PyResult<Vec<f64>> {
        let (dv, ov) = (f32s(decision, "decision")?, f32s(options, "options")?);
        py.detach(|| self.inner.logits(&dv, &ov)).map_err(err)
    }
    fn probabilities(&self, py: Python<'_>, decision: &[u8], options: &[u8]) -> PyResult<Vec<f64>> {
        let (dv, ov) = (f32s(decision, "decision")?, f32s(options, "options")?);
        py.detach(|| self.inner.probabilities(&dv, &ov)).map_err(err)
    }
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("CoreUnsupported", m.py().get_type::<CoreUnsupported>())?;
    m.add("METHODS", api::METHODS.to_vec())?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    m.add_function(wrap_pyfunction!(answers, m)?)?;
    m.add_function(wrap_pyfunction!(canonical, m)?)?;
    m.add_function(wrap_pyfunction!(canonical_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(digests, m)?)?;
    m.add_function(wrap_pyfunction!(verification, m)?)?;
    m.add_function(wrap_pyfunction!(lite, m)?)?;
    m.add_function(wrap_pyfunction!(export_filename, m)?)?;
    m.add_function(wrap_pyfunction!(route, m)?)?;
    m.add_function(wrap_pyfunction!(call, m)?)?;
    m.add_class::<PointerHead>()?;
    Ok(())
}
