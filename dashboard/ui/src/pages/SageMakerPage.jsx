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
import Prism from 'prismjs'
import 'prismjs/components/prism-python'
import 'prismjs/themes/prism.css'

const API = 'http://localhost:8000'

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
}
