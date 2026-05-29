import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Select from '@cloudscape-design/components/select'
import Tabs from '@cloudscape-design/components/tabs'
import Alert from '@cloudscape-design/components/alert'
import Badge from '@cloudscape-design/components/badge'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Multiselect from '@cloudscape-design/components/multiselect'
import SegmentedControl from '@cloudscape-design/components/segmented-control'
import FileUpload from '@cloudscape-design/components/file-upload'
import CodeEditor from '../components/CodeEditor'

const API = 'http://localhost:8000'

// python3.12 first — its base image is AL2023 (glibc 2.34), matching real AWS
// Lambda's modern runtime so pandas/numpy/SciPy wheels load. python3.11 is
// still on Amazon Linux 2 (glibc 2.26) — fine for pure-Python deps only.
const RUNTIMES = [
  { value: 'python3.12', label: 'python3.12' },
  { value: 'python3.11', label: 'python3.11 (pure-Python deps only)' },
  { value: 'python3.10', label: 'python3.10 (pure-Python deps only)' },
  { value: 'nodejs20.x', label: 'nodejs20.x' },
  { value: 'nodejs18.x', label: 'nodejs18.x' },
]

const langFor = (runtime) =>
  runtime?.startsWith('python') ? 'python'
  : runtime?.startsWith('nodejs') ? 'javascript'
  : 'python'

export default function LambdaPage() {
  const [functions, setFunctions] = useState([])
  const [layers, setLayers] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)        // function name
  const [detail, setDetail] = useState(null)            // detail JSON
  const [activeTab, setActiveTab] = useState('code')
  const [showCreate, setShowCreate] = useState(false)
  const [showLayer, setShowLayer] = useState(false)

  const refresh = (showLoading = true) => {
    if (showLoading) setLoading(true)
    Promise.all([
      fetch(`${API}/api/lambda/functions`).then(r => r.json()),
      fetch(`${API}/api/lambda/layers`).then(r => r.json()),
    ]).then(([fns, lyr]) => {
      setFunctions(fns.functions || [])
      setLayers(lyr.layers || [])
      setLoading(false)
    })
  }
  // Effect body just kicks off async work — all setState happens inside .then().
  useEffect(() => {
    Promise.all([
      fetch(`${API}/api/lambda/functions`).then(r => r.json()),
      fetch(`${API}/api/lambda/layers`).then(r => r.json()),
    ]).then(([fns, lyr]) => {
      setFunctions(fns.functions || [])
      setLayers(lyr.layers || [])
      setLoading(false)
    })
  }, [])

  const openFunction = (name) => {
    setSelected(name); setDetail(null); setActiveTab('code')
    fetch(`${API}/api/lambda/functions/${name}`).then(r => r.json()).then(setDetail)
  }
  const reloadDetail = () =>
    fetch(`${API}/api/lambda/functions/${selected}`).then(r => r.json()).then(setDetail)

  if (selected && detail && !detail.error) {
    return (
      <FunctionDetail
        detail={detail} layers={layers}
        onBack={() => { setSelected(null); setDetail(null); refresh() }}
        onReload={reloadDetail}
        activeTab={activeTab} setActiveTab={setActiveTab}
      />
    )
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="AWS Lambda. Functions execute for real — moto spawns a per-invoke container from ghcr.io/shogo82148/lambda-{runtime} (pulled lazily on first create, not via docker-compose). Defaults to x86_64 + python3.12 (AL2023, glibc 2.34) to match real AWS."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="add-plus" onClick={() => setShowLayer(true)}>Publish layer</Button>
            <Button variant="primary" iconName="add-plus" onClick={() => setShowCreate(true)}>Create function</Button>
            <Button iconName="refresh" onClick={refresh}>Refresh</Button>
          </SpaceBetween>
        }
      >
        AWS Lambda
      </Header>

      <Table
        header={<Header variant="h2" counter={`(${functions.length})`}>Functions</Header>}
        loading={loading}
        items={functions}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: i => (
            <Button variant="link" onClick={() => openFunction(i.name)}>{i.name}</Button>
          )},
          { id: 'runtime', header: 'Runtime', cell: i => <Badge>{i.runtime || '—'}</Badge> },
          { id: 'handler', header: 'Handler', cell: i => <Box variant="code" fontSize="body-s">{i.handler}</Box> },
          { id: 'memory', header: 'Memory', cell: i => `${i.memory} MB` },
          { id: 'timeout', header: 'Timeout', cell: i => `${i.timeout}s` },
          { id: 'layers', header: 'Layers', cell: i => i.layers.length },
          { id: 'lastModified', header: 'Last modified', cell: i => <Box fontSize="body-s">{i.lastModified}</Box> },
        ]}
        empty={<Box textAlign="center">No functions yet — create one to invoke.</Box>}
      />

      <Table
        header={<Header variant="h2" counter={`(${layers.length})`}>Layers</Header>}
        items={layers}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: i => <Box variant="strong">{i.name}</Box> },
          { id: 'version', header: 'Latest version', cell: i => i.latestVersion ?? '—' },
          { id: 'runtimes', header: 'Compatible runtimes', cell: i => (i.runtimes || []).join(', ') },
          { id: 'arn', header: 'Version ARN', cell: i => <Box variant="code" fontSize="body-s">{i.latestVersionArn}</Box> },
          { id: 'description', header: 'Description', cell: i => i.description },
        ]}
        empty={<Box textAlign="center">No layers yet.</Box>}
      />

      {showCreate && <CreateModal onClose={() => setShowCreate(false)} onCreated={refresh} />}
      {showLayer && <LayerModal onClose={() => setShowLayer(false)} onPublished={refresh} />}
    </SpaceBetween>
  )
}

function FunctionDetail({ detail, layers, onBack, onReload, activeTab, setActiveTab }) {
  const [source, setSource] = useState(detail.source || '')
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState(null)
  const [eventJson, setEventJson] = useState('{\n  "x": 7\n}')
  const [invokeResult, setInvokeResult] = useState(null)
  const [invoking, setInvoking] = useState(false)
  const [attached, setAttached] = useState(detail.layers || [])
  const [attachMsg, setAttachMsg] = useState(null)

  // Re-sync local edit buffers when the parent passes a new detail (e.g. after Save).
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setSource(detail.source || '') }, [detail.source])
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setAttached(detail.layers || []) }, [detail.layers])

  const save = async () => {
    setSaving(true); setSaveMsg(null)
    const r = await fetch(`${API}/api/lambda/functions/${detail.name}/code`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source }),
    }).then(r => r.json())
    setSaving(false)
    setSaveMsg(r.error ? { type: 'error', text: r.error } : { type: 'success', text: 'Saved + repacked.' })
    onReload()
  }

  const invoke = async () => {
    let payload
    try { payload = JSON.parse(eventJson || '{}') }
    catch (e) { setInvokeResult({ error: `Event is not valid JSON: ${e.message}` }); return }
    setInvoking(true); setInvokeResult(null)
    const r = await fetch(`${API}/api/lambda/functions/${detail.name}/invoke`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payload }),
    }).then(r => r.json())
    setInvoking(false)
    setInvokeResult(r)
  }

  const saveLayers = async () => {
    setAttachMsg(null)
    const r = await fetch(`${API}/api/lambda/functions/${detail.name}/layers`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ layers: attached }),
    }).then(r => r.json())
    setAttachMsg(r.error ? { type: 'error', text: r.error } : { type: 'success', text: 'Layers updated.' })
    onReload()
  }

  const language = langFor(detail.runtime)
  const layerOptions = layers.map(l => ({
    value: l.latestVersionArn, label: `${l.name}:${l.latestVersion}`,
    description: l.description || (l.runtimes || []).join(', '),
  }))

  const resultPayload = invokeResult?.payload
  const resultText = typeof resultPayload === 'string'
    ? resultPayload : (resultPayload ? JSON.stringify(resultPayload, null, 2) : '')

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          <SpaceBetween direction="horizontal" size="xs">
            <Badge>{detail.runtime}</Badge>
            <Box variant="code" fontSize="body-s">{detail.handler}</Box>
            <Box color="text-body-secondary" fontSize="body-s">
              {detail.memory} MB · {detail.timeout}s
            </Box>
          </SpaceBetween>
        }
        actions={<Button onClick={onBack} iconName="arrow-left">Back</Button>}
      >
        {detail.name}
      </Header>

      <Tabs
        activeTabId={activeTab}
        onChange={({ detail: d }) => setActiveTab(d.activeTabId)}
        tabs={[
          {
            id: 'code', label: 'Code',
            content: (
              <SpaceBetween size="m">
                <Box color="text-body-secondary" fontSize="body-s">
                  Editing <Box variant="code" fontSize="body-s">{detail.sourceFilename || 'handler'}</Box>. Save repackages a zip and calls UpdateFunctionCode.
                </Box>
                {detail.source == null && (
                  <Alert type="info">
                    No cached source yet — this function was created outside the dashboard. Paste new code below and Save.
                  </Alert>
                )}
                <CodeEditor value={source} onChange={setSource} language={language} rows={18} />
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="primary" loading={saving} onClick={save}>Save</Button>
                  {saveMsg && (
                    <StatusIndicator type={saveMsg.type === 'success' ? 'success' : 'error'}>
                      {saveMsg.text}
                    </StatusIndicator>
                  )}
                </SpaceBetween>
              </SpaceBetween>
            ),
          },
          {
            id: 'test', label: 'Test',
            content: (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, alignItems: 'stretch' }}>
                <Container fitHeight header={<Header variant="h3" description="Event JSON sent to the handler">Event</Header>}>
                  <SpaceBetween size="m">
                    <CodeEditor value={eventJson} onChange={setEventJson} language="json" rows={14} />
                    <Button variant="primary" loading={invoking} iconName="play" onClick={invoke}>Invoke</Button>
                  </SpaceBetween>
                </Container>
                <Container fitHeight header={
                  <Header variant="h3" actions={invokeResult && (
                    <SpaceBetween direction="horizontal" size="xs">
                      {invokeResult.functionError
                        ? <StatusIndicator type="error">{invokeResult.functionError}</StatusIndicator>
                        : <StatusIndicator type="success">200</StatusIndicator>}
                      <Box variant="code" fontSize="body-s">{invokeResult.durationMs} ms</Box>
                    </SpaceBetween>
                  )}>
                    Result
                  </Header>
                }>
                  {invokeResult?.error && <Alert type="error">{invokeResult.error}</Alert>}
                  {invokeResult?.functionError && (
                    <Alert type="warning">Function returned an error — see payload below.</Alert>
                  )}
                  {resultText
                    ? <CodeEditor value={resultText} onChange={() => {}} language="json" rows={10} readOnly />
                    : !invokeResult && <Box color="text-status-inactive" padding={{ vertical: 'l' }} textAlign="center">Click Invoke to run the handler.</Box>}
                  {invokeResult?.logTail && (
                    <Box padding={{ top: 's' }}>
                      <Box variant="awsui-key-label">Logs (tail)</Box>
                      <CodeEditor value={invokeResult.logTail} onChange={() => {}} language="javascript" rows={6} readOnly />
                    </Box>
                  )}
                </Container>
              </div>
            ),
          },
          {
            id: 'config', label: 'Configuration',
            content: (
              <Container>
                <SpaceBetween size="m">
                  <KV k="Function ARN (role)" v={detail.role} />
                  <KV k="Runtime" v={detail.runtime} />
                  <KV k="Handler" v={detail.handler} />
                  <KV k="Memory" v={`${detail.memory} MB`} />
                  <KV k="Timeout" v={`${detail.timeout} s`} />
                  <KV k="Code size" v={`${detail.codeSize} B`} />
                  <KV k="Last modified" v={detail.lastModified} />
                  <Box variant="awsui-key-label">Environment variables</Box>
                  {Object.keys(detail.envVars || {}).length
                    ? Object.entries(detail.envVars).map(([k, v]) => <KV key={k} k={k} v={v} />)
                    : <Box color="text-status-inactive">(none)</Box>}
                </SpaceBetween>
              </Container>
            ),
          },
          {
            id: 'layers', label: `Layers (${attached.length})`,
            content: (
              <Container>
                <SpaceBetween size="m">
                  <Box color="text-body-secondary" fontSize="body-s">
                    Layers are packaged with the function on invoke and unpacked into <Box variant="code" fontSize="body-s">/opt</Box> in the runtime container. Saving replaces the list (Lambda's API is set-not-append).
                  </Box>
                  <FormField label="Attached layer versions">
                    <Multiselect
                      selectedOptions={attached.map(a => ({
                        value: a,
                        label: layerOptions.find(o => o.value === a)?.label || a,
                      }))}
                      onChange={({ detail: d }) => setAttached(d.selectedOptions.map(o => o.value))}
                      options={layerOptions}
                      placeholder="Pick layers to attach"
                      empty="No layers published. Use 'Publish layer' on the Lambda list page."
                    />
                  </FormField>
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="primary" onClick={saveLayers}>Save layers</Button>
                    {attachMsg && (
                      <StatusIndicator type={attachMsg.type === 'success' ? 'success' : 'error'}>
                        {attachMsg.text}
                      </StatusIndicator>
                    )}
                  </SpaceBetween>
                </SpaceBetween>
              </Container>
            ),
          },
        ]}
      />
    </SpaceBetween>
  )
}

function KV({ k, v }) {
  return (
    <div>
      <Box variant="awsui-key-label">{k}</Box>
      <Box variant="code" fontSize="body-s">{String(v ?? '')}</Box>
    </div>
  )
}

function CreateModal({ onClose, onCreated }) {
  const [name, setName] = useState('hello')
  const [runtime, setRuntime] = useState({ value: 'python3.12', label: 'python3.12' })
  const [handler, setHandler] = useState('handler.handler')
  const [memory, setMemory] = useState('128')
  const [timeout, setTimeoutVal] = useState('10')
  const [creating, setCreating] = useState(false)
  const [err, setErr] = useState(null)

  const create = async () => {
    setCreating(true); setErr(null)
    const r = await fetch(`${API}/api/lambda/functions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, runtime: runtime.value, handler,
        memory: Number(memory), timeout: Number(timeout),
      }),
    }).then(r => r.json())
    setCreating(false)
    if (r.error) { setErr(r.error); return }
    onCreated(); onClose()
  }

  return (
    <Modal visible onDismiss={onClose} header="Create function" size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={creating} onClick={create}>Create</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        <FormField label="Function name">
          <Input value={name} onChange={({ detail }) => setName(detail.value)} />
        </FormField>
        <FormField label="Runtime">
          <Select selectedOption={runtime} onChange={({ detail }) => setRuntime(detail.selectedOption)}
            options={RUNTIMES} />
        </FormField>
        <FormField label="Handler" description="module.function — a starter body is generated for new functions">
          <Input value={handler} onChange={({ detail }) => setHandler(detail.value)} />
        </FormField>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <FormField label="Memory (MB)"><Input type="number" value={memory} onChange={({ detail }) => setMemory(detail.value)} /></FormField>
          <FormField label="Timeout (s)"><Input type="number" value={timeout} onChange={({ detail }) => setTimeoutVal(detail.value)} /></FormField>
        </div>
      </SpaceBetween>
    </Modal>
  )
}

function LayerModal({ onClose, onPublished }) {
  const [name, setName] = useState('oblako-shared')
  // 'inline' packs a single text file (tiny, pure-Python). 'zip' uploads a real
  // layer archive — the only way to ship compiled deps (numpy, pandas) that
  // blow past Lambda's ~50 MB inline limit.
  const [mode, setMode] = useState('inline')
  const [filename, setFilename] = useState('python/util.py')
  const [content, setContent] = useState("VERSION = '1.0'\n")
  const [zipFiles, setZipFiles] = useState([])
  const [runtimes, setRuntimes] = useState([{ value: 'python3.11', label: 'python3.11' }])
  const [description, setDescription] = useState('Shared utilities')
  const [publishing, setPublishing] = useState(false)
  const [progress, setProgress] = useState(null)
  const [err, setErr] = useState(null)

  const publish = async () => {
    setPublishing(true); setErr(null); setProgress(null)
    try {
      let body
      if (mode === 'zip') {
        const zip = zipFiles[0]
        if (!zip) { setErr('Pick a .zip file to upload first.'); setPublishing(false); return }
        // 1. Ask the API for a presigned PUT into S3Proxy.
        setProgress('Staging upload…')
        const stage = await fetch(`${API}/api/lambda/layers/s3-upload`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        }).then(r => r.json())
        if (stage.error) throw new Error(stage.error)
        // 2. Upload the zip bytes straight to S3Proxy (bypasses the API's body
        //    size limits — that's the whole point of the S3 path).
        setProgress(`Uploading ${(zip.size / 1e6).toFixed(1)} MB…`)
        const put = await fetch(stage.putUrl, { method: 'PUT', body: zip })
        if (!put.ok) throw new Error(`upload failed: HTTP ${put.status}`)
        // 3. Publish the layer version referencing the staged object.
        setProgress('Publishing layer…')
        body = {
          name, description, s3Bucket: stage.bucket, s3Key: stage.key,
          runtimes: runtimes.map(o => o.value),
        }
      } else {
        body = { name, filename, content, description, runtimes: runtimes.map(o => o.value) }
      }
      const r = await fetch(`${API}/api/lambda/layers`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(r => r.json())
      if (r.error) throw new Error(r.error)
      onPublished(); onClose()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setPublishing(false); setProgress(null)
    }
  }

  return (
    <Modal visible onDismiss={onClose} header="Publish layer version" size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={publishing} onClick={publish}>Publish</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        {progress && <Alert type="info">{progress}</Alert>}
        <FormField label="Layer name"><Input value={name} onChange={({ detail }) => setName(detail.value)} /></FormField>
        <FormField label="Source">
          <SegmentedControl
            selectedId={mode}
            onChange={({ detail }) => setMode(detail.selectedId)}
            options={[
              { id: 'inline', text: 'Inline file' },
              { id: 'zip', text: 'Upload .zip' },
            ]}
          />
        </FormField>
        {mode === 'inline' ? (
          <>
            <FormField label="File path inside layer" description="Lambda unpacks the layer into /opt, so python deps go under python/.">
              <Input value={filename} onChange={({ detail }) => setFilename(detail.value)} />
            </FormField>
            <FormField label="Content">
              <CodeEditor value={content} onChange={setContent} language="python" rows={8} />
            </FormField>
          </>
        ) : (
          <FormField label="Layer .zip"
            description="A layer archive with deps under python/ (e.g. python/lib/python3.12/site-packages/…). Build with the matching arch + glibc — see the page header.">
            <FileUpload
              value={zipFiles}
              onChange={({ detail }) => setZipFiles(detail.value)}
              accept=".zip,application/zip"
              constraintText="One .zip, staged to S3Proxy then referenced by the layer version."
              i18nStrings={{
                uploadButtonText: () => 'Choose .zip',
                dropzoneText: () => 'Drop .zip to upload',
                removeFileAriaLabel: i => `Remove file ${i + 1}`,
                limitShowFewer: 'Show fewer files',
                limitShowMore: 'Show more files',
                errorIconAriaLabel: 'Error',
              }}
            />
          </FormField>
        )}
        <FormField label="Compatible runtimes">
          <Multiselect selectedOptions={runtimes}
            onChange={({ detail }) => setRuntimes(detail.selectedOptions)}
            options={RUNTIMES} />
        </FormField>
        <FormField label="Description">
          <Input value={description} onChange={({ detail }) => setDescription(detail.value)} />
        </FormField>
      </SpaceBetween>
    </Modal>
  )
}
