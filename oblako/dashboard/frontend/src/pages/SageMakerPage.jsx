import { useState, useEffect, useRef } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Alert from '@cloudscape-design/components/alert'
import Tabs from '@cloudscape-design/components/tabs'
import Link from '@cloudscape-design/components/link'
import Spinner from '@cloudscape-design/components/spinner'
import Prism from 'prismjs'
import 'prismjs/components/prism-python'
import 'prismjs/themes/prism.css'
import NotebookPage from './NotebookPage'

const API = 'http://localhost:8000'

// Create + wait flow for the local MLflow tracking server.
// Mirrors AWS SageMaker's CreateMlflowTrackingServer: an explicit Create press
// kicks off the App; the UI polls /api/mlflow/status until ready, then embeds.
function MlflowTab() {
  const [status, setStatus] = useState('idle')   // idle | starting | ready | error
  const [urls, setUrls] = useState({ url: null, vanityUrl: null, hostsLine: null, arn: null, customEndpoint: null })
  const [error, setError] = useState(null)

  // Probe once on mount: if MLflow was already started this session, jump
  // straight to "ready" without forcing the user to click Create again.
  useEffect(() => {
    fetch(`${API}/api/mlflow/status`).then(r => r.json()).then(d => {
      if (d.status === 'ready') {
        setStatus('ready')
        setUrls({ url: d.url, vanityUrl: d.vanityUrl, hostsLine: d.hostsLine, arn: d.arn, customEndpoint: d.customEndpoint })
      }
    }).catch(() => {})
  }, [])

  const createApp = async () => {
    setStatus('starting'); setError(null)
    try {
      const d = await fetch(`${API}/api/mlflow/launch`, { method: 'POST' }).then(r => r.json())
      if (d.status === 'ready') {
        setStatus('ready')
        setUrls({ url: d.url, vanityUrl: d.vanityUrl, hostsLine: d.hostsLine, arn: d.arn, customEndpoint: d.customEndpoint })
      } else {
        setStatus('error'); setError(d.error || 'MLflow did not become ready')
      }
    } catch (e) {
      setStatus('error'); setError(String(e?.message || e))
    }
  }

  if (status === 'idle') {
    return (
      <Container header={<Header variant="h2" description="Real AWS exposes MLflow as a SageMaker App you must create. Same flow here — Create starts the container and waits for it to become healthy.">Create MLflow tracking server</Header>}>
        <SpaceBetween size="m">
          <Box color="text-body-secondary">
            First launch pulls the MLflow image (~1 minute). Subsequent presses are near-instant.
          </Box>
          <Button variant="primary" iconName="add-plus" onClick={createApp}>Create MLflow App</Button>
        </SpaceBetween>
      </Container>
    )
  }
  if (status === 'starting') {
    return (
      <Container header={<Header variant="h2">Starting MLflow App…</Header>}>
        <Box padding="l"><Spinner /> Building image + starting container (first time ~1 min)</Box>
      </Container>
    )
  }
  if (status === 'error') {
    return (
      <SpaceBetween size="m">
        <Alert type="error" header="Could not start MLflow">{error}</Alert>
        <Button onClick={createApp} iconName="refresh">Retry</Button>
      </SpaceBetween>
    )
  }
  // Open the direct localhost URL — it always works. The vanity SageMaker-style
  // host needs an /etc/hosts entry, so it's shown only as an optional note.
  const webUrl = urls.url || urls.vanityUrl
  return (
    <SpaceBetween size="l">
      <Container header={
        <Header variant="h2"
          description="Point MLflow at the tracking-server ARN — exactly like real SageMaker. The sagemaker-mlflow plugin resolves the ARN and SigV4-signs the traffic.">
          Tracking server ARN
        </Header>
      }>
        <SpaceBetween size="m">
          {urls.arn && <Box variant="code" fontSize="body-s">{urls.arn}</Box>}
          <PythonCode code={`import mlflow

mlflow.set_tracking_uri(\n    "${urls.arn || 'arn:aws:sagemaker:...:mlflow-tracking-server/mlflow-oblako'}"\n)
mlflow.set_experiment("my-experiment")
with mlflow.start_run():
    mlflow.log_metric("rmse", 0.1)`} />
          <Box color="text-body-secondary" fontSize="body-s">
            In an oblako notebook this works as-is. Elsewhere, set{' '}
            <Box variant="code" fontSize="body-s" display="inline">
              SAGEMAKER_MLFLOW_CUSTOM_ENDPOINT={urls.customEndpoint || urls.url}
            </Box>{' '}so the plugin resolves the ARN to the local server.
          </Box>
        </SpaceBetween>
      </Container>

      {/* No iframe: MLflow sends X-Frame-Options: SAMEORIGIN, so it refuses to
          render cross-origin from the dashboard. Link out instead. */}
      <Container header={
        <Header variant="h2"
          actions={<Button iconName="external" href={webUrl} target="_blank">Open MLflow UI</Button>}
          description="The MLflow tracking UI — experiments, runs, models and the model registry.">
          Web UI
        </Header>
      }>
        <SpaceBetween size="xs">
          <Box>
            <strong>URL:</strong> <Link external href={webUrl}>{webUrl}</Link>
          </Box>
          {urls.vanityUrl && urls.hostsLine && (
            <Box color="text-body-secondary" fontSize="body-s">
              SageMaker-style address: <Box variant="code" fontSize="body-s" display="inline">{urls.vanityUrl}</Box>
              {' '}— to use it, add to <Box variant="code" fontSize="body-s" display="inline">/etc/hosts</Box>:{' '}
              <Box variant="code" fontSize="body-s" display="inline">{urls.hostsLine}</Box>
            </Box>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  )
}

const SAGEMAKER_CODE = `from sagemaker.local import LocalSession
from sagemaker.estimator import Estimator

session = LocalSession()
estimator = Estimator(
    image_uri="my-training-image:latest",
    instance_type="local",
    sagemaker_session=session,
    output_path="s3://my-bucket/models",
)
estimator.fit({"train": "s3://my-bucket/data/train.csv"})`

function PythonCode({ code }) {
  const ref = useRef(null)
  useEffect(() => {
    if (ref.current) Prism.highlightElement(ref.current)
  }, [code])
  return (
    <pre style={{ margin: 0, borderRadius: 6, overflow: 'auto' }}>
      <code ref={ref} className="language-python">{code}</code>
    </pre>
  )
}

const DOMAIN = 'studio'

function StudioTab() {
  const [domain, setDomain] = useState(null)
  const [busy, setBusy] = useState(false)
  const [nb, setNb] = useState(null)
  const [err, setErr] = useState(null)

  const refresh = () =>
    fetch(`${API}/api/sagemaker/domains/${DOMAIN}/status`).then(r => r.json()).then(setDomain)
  // Effect body just kicks off async work — all setState happens inside .then().
  useEffect(() => {
    fetch(`${API}/api/sagemaker/domains/${DOMAIN}/status`).then(r => r.json()).then(setDomain)
  }, [])

  const create = async () => {
    setBusy(true); setErr(null)
    const r = await fetch(`${API}/api/sagemaker/domains`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: DOMAIN }),
    }).then(r => r.json())
    setBusy(false)
    if (r.status === 'error') { setErr(r.error); return }
    setDomain(r)
  }
  const openNotebook = async () => {
    setBusy(true); setErr(null)
    const r = await fetch(`${API}/api/sagemaker/domains/${DOMAIN}/notebook`, { method: 'POST' })
      .then(r => r.json())
    setBusy(false)
    if (r.error) { setErr(r.error); return }
    setNb(r); window.open(r.url, '_blank')
  }
  const del = async () => {
    setBusy(true); setErr(null)
    await fetch(`${API}/api/sagemaker/domains/${DOMAIN}`, { method: 'DELETE' })
    setBusy(false); setNb(null); refresh()
  }

  const exists = domain && domain.status !== 'NONE' && domain.status !== 'error'
  const ready = domain?.status === 'CREATE_COMPLETE' || domain?.status === 'UPDATE_COMPLETE'

  return (
    <SpaceBetween size="l">
      <Container header={
        <Header variant="h2"
          description="A Studio domain is provisioned as a CloudFormation stack: an S3 artifacts bucket + an EC2 notebook instance (a real container) with an EBS volume. The notebook runs JupyterLab inside that instance, pre-wired to oblako's services."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {!exists && <Button variant="primary" loading={busy} onClick={create}>Create domain</Button>}
              {ready && <Button variant="primary" loading={busy} iconName="external" onClick={openNotebook}>Open notebook</Button>}
              {exists && <Button loading={busy} onClick={del}>Delete domain</Button>}
              <Button iconName="refresh" onClick={refresh}>Refresh</Button>
            </SpaceBetween>
          }>
          Studio domain
        </Header>
      }>
        <SpaceBetween size="m">
          {err && <Alert type="error">{err}</Alert>}
          {!domain && <Box><Spinner /> Checking…</Box>}
          {domain && !exists && (
            <Alert type="info">No domain yet. Create one — it deploys a CloudFormation stack (S3 + EC2 + EBS).</Alert>
          )}
          {exists && (
            <ColumnLayout columns={2} variant="text-grid">
              <div><Box variant="awsui-key-label">Status</Box>
                <StatusIndicator type={ready ? 'success' : 'in-progress'}>{domain.status}</StatusIndicator></div>
              <div><Box variant="awsui-key-label">CFN stack</Box><Box variant="code">{domain.stack}</Box></div>
              <div><Box variant="awsui-key-label">Artifacts bucket</Box><Box variant="code">{domain.artifactsBucket}</Box></div>
              <div><Box variant="awsui-key-label">Notebook instance</Box><Box variant="code">{domain.instanceId || '—'}</Box></div>
            </ColumnLayout>
          )}
          {nb && (
            <Alert type="success" header="JupyterLab launched on the notebook instance">
              <Link external href={nb.url}>{nb.url}</Link> — runs inside instance {nb.instanceId},
              EBS-backed home, boto3 pre-wired to oblako.
            </Alert>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  )
}

export default function SageMakerPage() {
  const [containers, setContainers] = useState({ training: [], endpoints: [] })
  const [images, setImages] = useState([])
  const [loading, setLoading] = useState(true)
  const [cleanupResult, setCleanupResult] = useState(null)

  const fetchData = () => {
    setLoading(true)
    Promise.all([
      fetch(`${API}/api/sagemaker/containers`).then(r => r.json()),
      fetch(`${API}/api/sagemaker/images`).then(r => r.json()),
    ]).then(([c, i]) => {
      setContainers(c)
      setImages(i.images || [])
      setLoading(false)
    }).catch(() => setLoading(false))
  }

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { fetchData() }, [])

  const cleanup = () => {
    fetch(`${API}/api/sagemaker/cleanup`, { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        setCleanupResult(data)
        fetchData()
      })
  }

  const totalContainers = containers.training.length + containers.endpoints.length

  const trainingTab = (
    <SpaceBetween size="l">
      {cleanupResult && (
        <Alert type="success" dismissible onDismiss={() => setCleanupResult(null)}>
          Removed {cleanupResult.removed} stopped container(s).
        </Alert>
      )}

      <ColumnLayout columns={3}>
        <Container>
          <Box variant="awsui-key-label">Mode</Box>
          <Box variant="awsui-value-large">Local</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Active containers</Box>
          <Box variant="awsui-value-large">{totalContainers}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Docker images</Box>
          <Box variant="awsui-value-large">{images.length}</Box>
        </Container>
      </ColumnLayout>

      <Table
        header={<Header variant="h2">Training jobs</Header>}
        loading={loading}
        items={containers.training}
        columnDefinitions={[
          { id: 'name', header: 'Container', cell: item => <Box fontWeight="bold">{item.name}</Box> },
          { id: 'id', header: 'ID', cell: item => <Box variant="code">{item.id}</Box> },
          { id: 'status', header: 'Status', cell: item => (
            <StatusIndicator type={item.status === 'running' ? 'in-progress' : 'stopped'}>{item.status}</StatusIndicator>
          )},
          { id: 'image', header: 'Image', cell: item => (item.image || []).join(', ') || '-' },
        ]}
        empty={<Box textAlign="center">No training jobs running. Use SageMaker local mode to start one.</Box>}
      />

      <Table
        header={<Header variant="h2">Endpoints</Header>}
        loading={loading}
        items={containers.endpoints}
        columnDefinitions={[
          { id: 'name', header: 'Container', cell: item => <Box fontWeight="bold">{item.name}</Box> },
          { id: 'id', header: 'ID', cell: item => <Box variant="code">{item.id}</Box> },
          { id: 'status', header: 'Status', cell: item => (
            <StatusIndicator type={item.status === 'running' ? 'success' : 'stopped'}>{item.status}</StatusIndicator>
          )},
          { id: 'ports', header: 'Ports', cell: item => JSON.stringify(item.ports || {}) },
        ]}
        empty={<Box textAlign="center">No endpoints deployed.</Box>}
      />

      <Table
        header={<Header variant="h2">Docker images</Header>}
        loading={loading}
        items={images}
        columnDefinitions={[
          { id: 'tags', header: 'Tags', cell: item => (item.tags || []).join(', ') || '-' },
          { id: 'id', header: 'ID', cell: item => <Box variant="code">{item.id}</Box> },
          { id: 'size', header: 'Size', cell: item => item.size },
        ]}
        empty={<Box textAlign="center">No SageMaker images found. Build one with: sagemaker.build_image()</Box>}
      />

      <Container header={<Header variant="h2">Quick start</Header>}>
        <PythonCode code={SAGEMAKER_CODE} />
      </Container>
    </SpaceBetween>
  )

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={
        <SpaceBetween direction="horizontal" size="s">
          <Button onClick={fetchData} iconName="refresh">Refresh</Button>
          <Button onClick={cleanup}>Cleanup stopped</Button>
        </SpaceBetween>
      }>
        Amazon SageMaker
      </Header>
      <Tabs tabs={[
        { id: 'studio', label: 'Studio domain', content: <StudioTab /> },
        { id: 'notebook', label: 'Notebook', content: <NotebookPage embedded /> },
        { id: 'mlflow', label: 'MLflow', content: <MlflowTab /> },
        { id: 'training', label: 'Training & endpoints', content: trainingTab },
      ]} />
    </SpaceBetween>
  )
}
