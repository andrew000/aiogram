use std::sync::Arc;
use std::time::Duration;

use pyo3::exceptions::{PyRuntimeError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyModule};
use pyo3::{Bound, PyErr, PyResult, Python, create_exception};
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use reqwest::multipart::{Form, Part};
use reqwest::{Client, Proxy, Response};
use tokio::sync::Mutex;

create_exception!(
    aiogram.client.session._rust,
    RustTransportError,
    pyo3::exceptions::PyException
);

#[pyclass(module = "aiogram.client.session._rust")]
struct RustHttpClient {
    client: Option<Client>,
    limit: usize,
    user_agent: Option<String>,
    proxy: Option<String>,
}

#[pyclass(module = "aiogram.client.session._rust")]
struct RustByteStream {
    state: Arc<Mutex<StreamState>>,
}

struct StreamState {
    response: Option<Response>,
    buffer: Vec<u8>,
}

#[pymethods]
impl RustHttpClient {
    #[new]
    #[pyo3(signature = (limit = 100, user_agent = None, proxy = None))]
    fn new(limit: usize, user_agent: Option<String>, proxy: Option<String>) -> PyResult<Self> {
        let client = build_client(limit, user_agent.as_deref(), proxy.as_deref())?;
        Ok(Self {
            client: Some(client),
            limit,
            user_agent,
            proxy,
        })
    }

    fn close(&mut self) {
        self.client = None;
    }

    #[pyo3(signature = (url, timeout, fields, files))]
    fn post<'py>(
        &self,
        py: Python<'py>,
        url: String,
        timeout: f64,
        fields: Vec<(String, String)>,
        files: Vec<(String, String, Vec<u8>)>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let request = client.post(url).timeout(duration_from_seconds(timeout)?);
            let response = if files.is_empty() {
                request.form(&fields).send().await
            } else {
                let mut form = Form::new();
                for (name, value) in fields {
                    form = form.text(name, value);
                }
                for (name, filename, data) in files {
                    let part = Part::bytes(data).file_name(filename);
                    form = form.part(name, part);
                }
                request.multipart(form).send().await
            }
            .map_err(map_reqwest_error)?;

            let status = response.status().as_u16();
            let text = response.text().await.map_err(map_reqwest_error)?;

            Python::attach(|py| Ok((status, text).into_pyobject(py)?.unbind()))
        })
    }

    #[pyo3(signature = (url, headers, timeout, raise_for_status = true))]
    fn stream_content<'py>(
        &self,
        py: Python<'py>,
        url: String,
        headers: Vec<(String, String)>,
        timeout: f64,
        raise_for_status: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let response = client
                .get(url)
                .headers(build_headers(headers)?)
                .timeout(duration_from_seconds(timeout)?)
                .send()
                .await
                .map_err(map_reqwest_error)?;

            let response = if raise_for_status {
                response.error_for_status().map_err(map_reqwest_error)?
            } else {
                response
            };

            let stream = RustByteStream {
                state: Arc::new(Mutex::new(StreamState {
                    response: Some(response),
                    buffer: Vec::new(),
                })),
            };

            Python::attach(|py| Ok(Py::new(py, stream)?.into_any()))
        })
    }
}

#[pymethods]
impl RustByteStream {
    #[pyo3(signature = (chunk_size = 65536, max_chunks = 16))]
    fn next_chunks<'py>(
        &self,
        py: Python<'py>,
        chunk_size: usize,
        max_chunks: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        if chunk_size == 0 {
            return Err(PyValueError::new_err(
                "chunk_size must be greater than zero",
            ));
        }
        if max_chunks == 0 {
            return Err(PyValueError::new_err(
                "max_chunks must be greater than zero",
            ));
        }

        let state = Arc::clone(&self.state);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut state = state.lock().await;
            let mut chunks = Vec::with_capacity(max_chunks);

            while chunks.len() < max_chunks {
                while state.buffer.len() < chunk_size {
                    let Some(response) = state.response.as_mut() else {
                        break;
                    };
                    match response.chunk().await.map_err(map_reqwest_error)? {
                        Some(chunk) => state.buffer.extend_from_slice(&chunk),
                        None => {
                            state.response = None;
                            break;
                        }
                    }
                }

                if state.buffer.is_empty() {
                    break;
                }

                let take = chunk_size.min(state.buffer.len());
                chunks.push(state.buffer.drain(..take).collect::<Vec<u8>>());

                if state.response.is_none() && state.buffer.is_empty() {
                    break;
                }
            }

            let done = state.response.is_none() && state.buffer.is_empty();

            Python::attach(|py| {
                let list = PyList::empty(py);
                for chunk in chunks {
                    list.append(PyBytes::new(py, &chunk))?;
                }
                Ok((list, done).into_pyobject(py)?.unbind().into_any())
            })
        })
    }
}

impl RustHttpClient {
    fn client(&self) -> PyResult<Client> {
        match &self.client {
            Some(client) => Ok(client.clone()),
            None => build_client(
                self.limit,
                self.user_agent.as_deref(),
                self.proxy.as_deref(),
            ),
        }
    }
}

fn build_client(limit: usize, user_agent: Option<&str>, proxy: Option<&str>) -> PyResult<Client> {
    let mut builder = Client::builder().pool_max_idle_per_host(limit);
    if let Some(user_agent) = user_agent {
        builder = builder.user_agent(user_agent);
    }
    if let Some(proxy) = proxy {
        builder = builder.proxy(
            Proxy::all(proxy)
                .map_err(|error| PyValueError::new_err(format!("Invalid proxy URL: {error}")))?,
        );
    }
    builder
        .build()
        .map_err(|error| PyRuntimeError::new_err(format!("Failed to create HTTP client: {error}")))
}

fn duration_from_seconds(timeout: f64) -> PyResult<Duration> {
    if timeout <= 0.0 {
        return Err(PyValueError::new_err("Timeout must be greater than zero"));
    }
    Ok(Duration::from_secs_f64(timeout))
}

fn build_headers(headers: Vec<(String, String)>) -> PyResult<HeaderMap> {
    let mut header_map = HeaderMap::new();
    for (name, value) in headers {
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|error| PyValueError::new_err(format!("Invalid header name: {error}")))?;
        let value = HeaderValue::from_str(&value)
            .map_err(|error| PyValueError::new_err(format!("Invalid header value: {error}")))?;
        header_map.insert(name, value);
    }
    Ok(header_map)
}

fn map_reqwest_error(error: reqwest::Error) -> PyErr {
    if error.is_timeout() {
        return PyTimeoutError::new_err("Request timeout error");
    }
    RustTransportError::new_err(error.to_string())
}

#[pymodule]
fn _rust(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RustHttpClient>()?;
    module.add_class::<RustByteStream>()?;
    module.add("RustTransportError", py.get_type::<RustTransportError>())?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
