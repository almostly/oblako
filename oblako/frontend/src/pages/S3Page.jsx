import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import BreadcrumbGroup from '@cloudscape-design/components/breadcrumb-group'
import Button from '@cloudscape-design/components/button'

const API = 'http://localhost:8000'

export default function S3Page() {
  const [buckets, setBuckets] = useState([])
  const [selectedBucket, setSelectedBucket] = useState(null)
  const [objects, setObjects] = useState([])
  const [prefix, setPrefix] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`${API}/api/s3/buckets`)
      .then(r => r.json())
      .then(data => { setBuckets(data.buckets || []); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  const openBucket = (name) => {
    setSelectedBucket(name)
    setPrefix('')
    fetchObjects(name, '')
  }

  const fetchObjects = (bucket, pfx) => {
    setLoading(true)
    fetch(`${API}/api/s3/buckets/${bucket}?prefix=${encodeURIComponent(pfx)}`)
      .then(r => r.json())
      .then(data => {
        setObjects(data.objects || [])
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }

  const openPrefix = (pfx) => {
    setPrefix(pfx)
    fetchObjects(selectedBucket, pfx)
  }

  if (!selectedBucket) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Amazon S3</Header>
        <Table
          header={<Header variant="h2">Buckets</Header>}
          loading={loading}
          items={buckets}
          columnDefinitions={[
            { id: 'name', header: 'Name', cell: item => <Button variant="link" onClick={() => openBucket(item.Name)}>{item.Name}</Button> },
            { id: 'created', header: 'Creation date', cell: item => item.CreationDate },
          ]}
          empty={<Box textAlign="center">No buckets. Create one via boto3.</Box>}
        />
      </SpaceBetween>
    )
  }

  const breadcrumbs = [
    { text: 'S3', href: '#' },
    { text: selectedBucket, href: '#' },
  ]
  if (prefix) {
    prefix.split('/').filter(Boolean).forEach((part, i, arr) => {
      breadcrumbs.push({ text: part, href: '#' })
    })
  }

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={breadcrumbs}
        onFollow={e => {
          e.preventDefault()
          if (e.detail.text === 'S3') { setSelectedBucket(null) }
          else if (e.detail.text === selectedBucket) { openPrefix('') }
        }}
      />
      <Header variant="h1">{selectedBucket}</Header>
      <Table
        header={<Header variant="h2">Objects {prefix && `(${prefix})`}</Header>}
        loading={loading}
        items={objects}
        columnDefinitions={[
          { id: 'key', header: 'Key', cell: item => item.Key },
          { id: 'size', header: 'Size', cell: item => `${item.Size} bytes` },
          { id: 'modified', header: 'Last modified', cell: item => item.LastModified },
        ]}
        empty={<Box textAlign="center">No objects in this bucket.</Box>}
      />
    </SpaceBetween>
  )
}
