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

// Launches the local MLflow tracking server (oblako.mlflow) and embeds its UI.
function MlflowTab() {
  const [state, setState] = useState({ loading: true, url: null, vanityUrl: null, hostsLine: null, error: null })

  useEffect(() => {
    let active = true
    setState({ loading: true, url: null, vanityUrl: null, hostsLine: null, error: null })
    fetch(`${API}/api/mlflow/launch`, { method: 'POST' })
      .then(r => r.json())
      .then(d => { if (active) setState({ loading: false, url: d.url || null, vanityUrl: d.vanityUrl || null, hostsLine: d.hostsLine || null, error: d.error || null }) })
      .catch(e => { if (active) setState({ loading: false, url: null, vanityUrl: null, hostsLine: null, error: e.message }) })
    return () => { active = false }
  }, [])

  if (state.loading) {
    return <Box padding="l"><Spinner /> Starting MLflow App… (serverless — no servers to manage; first launch builds the App image, ~a minute)</Box>
  }
  if (state.error) {
    return <Box padding="l" color="text-status-error">{state.error}</Box>
  }
  return (
    <SpaceBetween size="s">
      {state.vanityUrl && (
        <Box padding="s" color="text-body-secondary" fontSize="body-s"
             variant="div">
          <strong>Tracking server URL:</strong>{' '}
          <Box variant="code" fontSize="body-s">{state.vanityUrl}</Box>
          {state.hostsLine && (
            <Box variant="div" padding={{ top: 'xxs' }}>
              First time only — add to <Box variant="code" fontSize="body-s">/etc/hosts</Box>:{' '}
              <Box variant="code" fontSize="body-s">{state.hostsLine}</Box>
            </Box>
          )}
        </Box>
      )}
      <Box float="right"><Link external href={state.vanityUrl || state.url}>Open in a new tab</Link></Box>
      <iframe
        title="MLflow"
        src={state.url}
        style={{ width: '100%', height: '78vh', border: '1px solid #d5dbdb', borderRadius: 4 }}
      />
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
        { id: 'notebook', label: 'Notebook', content: <NotebookPage embedded /> },
        { id: 'mlflow', label: 'MLflow', content: <MlflowTab /> },
        { id: 'training', label: 'Training & endpoints', content: trainingTab },
      ]} />
    </SpaceBetween>
  )
}
