import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import StatusIndicator from '@cloudscape-design/components/status-indicator'

const API = 'http://localhost:8000'

export default function DynamoDBPage() {
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState(null)
  const [items, setItems] = useState([])
  const [tableInfo, setTableInfo] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchTables = () => {
    setLoading(true)
    fetch(`${API}/api/dynamodb/tables`)
      .then(r => r.json())
      .then(data => { setTables(data.tables || []); setLoading(false) })
      .catch(() => setLoading(false))
  }

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { fetchTables() }, [])

  const openTable = (name) => {
    setSelectedTable(name)
    fetch(`${API}/api/dynamodb/tables/${name}`)
      .then(r => r.json())
      .then(data => { setTableInfo(data.table || null); setItems(data.items || []) })
  }

  if (selectedTable && tableInfo) {
    const columns = items.length > 0 ? Object.keys(items[0]) : []
    return (
      <SpaceBetween size="l">
        <Header variant="h1" actions={<Button onClick={() => setSelectedTable(null)}>Back to tables</Button>}>
          {selectedTable}
        </Header>

        <ColumnLayout columns={3}>
          <Container>
            <Box variant="awsui-key-label">Status</Box>
            <StatusIndicator type="success">{tableInfo.TableStatus}</StatusIndicator>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Item count</Box>
            <Box variant="awsui-value-large">{tableInfo.ItemCount}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Key schema</Box>
            <Box variant="p">
              {(tableInfo.KeySchema || []).map(k => `${k.AttributeName} (${k.KeyType})`).join(', ')}
            </Box>
          </Container>
        </ColumnLayout>

        <Table
          header={<Header variant="h2">Items (first 50)</Header>}
          items={items}
          columnDefinitions={columns.map(col => ({
            id: col,
            header: col,
            cell: item => {
              const val = item[col]
              return typeof val === 'object' ? JSON.stringify(val) : String(val ?? '')
            },
          }))}
          empty={<Box textAlign="center">No items in this table.</Box>}
        />
      </SpaceBetween>
    )
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={<Button onClick={fetchTables} iconName="refresh">Refresh</Button>}>
        Amazon DynamoDB
      </Header>
      <Table
        header={<Header variant="h2">Tables</Header>}
        loading={loading}
        items={tables.map(t => ({ name: t }))}
        columnDefinitions={[
          {
            id: 'name', header: 'Table name',
            cell: item => <Button variant="link" onClick={() => openTable(item.name)}>{item.name}</Button>
          },
        ]}
        empty={<Box textAlign="center">No tables. Create one via boto3.</Box>}
      />
    </SpaceBetween>
  )
}
