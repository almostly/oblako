import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Container from '@cloudscape-design/components/container'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import Textarea from '@cloudscape-design/components/textarea'
import Select from '@cloudscape-design/components/select'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

const API = 'http://localhost:8000'

export default function BedrockPage() {
  const [models, setModels] = useState([])
  const [selectedModel, setSelectedModel] = useState(null)
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetch(`${API}/api/bedrock/models`)
      .then(r => r.json())
      .then(data => {
        const m = data.models || []
        setModels(m)
        if (m.length > 0) setSelectedModel({ label: m[0].modelId, value: m[0].modelId })
      })
  }, [])

  const sendMessage = () => {
    if (!input.trim() || !selectedModel) return
    const userMsg = { role: 'user', content: [{ text: input }] }
    const newMessages = [...messages, userMsg]
    setMessages(newMessages)
    setInput('')
    setLoading(true)

    fetch(`${API}/api/bedrock/converse`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        modelId: selectedModel.value,
        messages: newMessages,
        inferenceConfig: { maxTokens: 512 },
      }),
    })
      .then(r => r.json())
      .then(data => {
        if (data.output?.message) {
          setMessages(prev => [...prev, data.output.message])
        }
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1">Amazon Bedrock</Header>

      <ColumnLayout columns={2}>
        <Container header={<Header variant="h2">Model</Header>}>
          <Select
            selectedOption={selectedModel}
            onChange={({ detail }) => setSelectedModel(detail.selectedOption)}
            options={models.map(m => ({ label: m.modelId, value: m.modelId }))}
            placeholder="Select a model"
          />
        </Container>
        <Container header={<Header variant="h2">Provider</Header>}>
          <Box variant="p">Ollama (local)</Box>
        </Container>
      </ColumnLayout>

      <Container header={<Header variant="h2">Chat playground</Header>}>
        <SpaceBetween size="m">
          <div style={{ minHeight: 200, maxHeight: 400, overflow: 'auto', padding: '8px', background: '#fafafa', borderRadius: 4 }}>
            {messages.length === 0 && <Box color="text-body-secondary">Start a conversation...</Box>}
            {messages.map((msg, i) => (
              <div key={i} style={{ marginBottom: 12 }}>
                <Box fontWeight="bold" color={msg.role === 'user' ? 'text-status-info' : 'text-status-success'}>
                  {msg.role === 'user' ? 'You' : 'Assistant'}
                </Box>
                <div className="oblako-md">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm, remarkMath]}
                    rehypePlugins={[rehypeKatex]}
                  >
                    {(msg.content || []).map(c => c.text || '').join('')}
                  </ReactMarkdown>
                </div>
              </div>
            ))}
            {loading && <Box color="text-body-secondary">Thinking...</Box>}
          </div>

          <Textarea
            value={input}
            onChange={({ detail }) => setInput(detail.value)}
            placeholder="Type a message..."
            rows={3}
            onKeyDown={e => { if (e.detail.key === 'Enter' && !e.detail.shiftKey) { e.preventDefault(); sendMessage() } }}
          />
          <Button variant="primary" onClick={sendMessage} loading={loading}>Send</Button>
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  )
}
