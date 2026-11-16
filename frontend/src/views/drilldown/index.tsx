import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/drilldown'
const columns = [
  "路径编号",
  "起始维度",
  "下钻层级序列",
  "每层聚合粒度",
  "默认度量",
  "路径深度",
  "缓存秒数",
  "数据新鲜度"
]
const statuses = [
  "草稿",
  "路径评审中",
  "已启用",
  "已归档"
]
const GRANULARITIES = ["年", "季", "月", "周", "天", "小时", "分钟", "秒"]
// 状态只能按 评审 -> 启用 -> 归档 推进，已归档为终态
const STATUS_ACTIONS: Record<string, string[]> = {
  "草稿": ["提交路径评审", "归档路径"],
  "路径评审中": ["启用路径", "归档路径"],
  "已启用": ["归档路径"],
  "已归档": [],
}

type Row = Record<string, any>

type LayerForm = {
  维度: string
  聚合粒度: string
  默认度量: string
  缓存秒数: string
  数据新鲜度: string
}

type AggDetail = {
  状态: string
  快照版本: number | null
  映射版本: number | null
  当前映射版本: number
  口径一致: boolean | null
  说明: string
  layers: Array<Record<string, any>>
}

const emptyLayer = (): LayerForm => ({ 维度: '', 聚合粒度: '天', 默认度量: '', 缓存秒数: '300', 数据新鲜度: '' })

export default function DrilldownView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [keyword, setKeyword] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [pathCode, setPathCode] = useState('')
  const [startDimension, setStartDimension] = useState('')
  const [layers, setLayers] = useState<LayerForm[]>([emptyLayer()])
  const [submitting, setSubmitting] = useState(false)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [aggDetails, setAggDetails] = useState<Record<number, AggDetail>>({})

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (keyword) query.set('keyword', keyword)
    if (statusFilter) query.set('status', statusFilter)
    query.set('size', '200')
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('下钻路径列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻列表读取失败')
    }
  }, [keyword, statusFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => [
    { label: '下钻路径总数', value: total },
    { label: '已启用路径', value: rows.filter((row) => row.status === '已启用').length },
    { label: '评审中路径', value: rows.filter((row) => row.status === '路径评审中').length },
  ], [rows, total])

  async function parsePayload(response: Response) {
    const payload = await response.json().catch(() => ({}))
    if (response.status === 409) {
      throw new Error(payload.detail ?? '启用冲突：另一请求已生效，请刷新查看最新快照')
    }
    if (!response.ok) {
      throw new Error(payload.detail ?? '多维下钻操作失败，请稍后重试')
    }
    if (payload.ok === false) {
      throw new Error(payload.message ?? '多维下钻操作未生效')
    }
    return payload
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, expected_version: row.版本 } }),
      })
      const payload = await parsePayload(response)
      setNotice(payload.message ?? `下钻路径已${action}`)
      setAggDetails({})
      setExpandedId(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻操作失败')
    }
  }

  async function convertLegacy(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/convert`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await parsePayload(response)
      setNotice(payload.message ?? '旧路径已完成兼容转换')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '兼容转换失败')
    }
  }

  async function submitCreate() {
    setSubmitting(true)
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            路径编号: pathCode,
            起始维度: startDimension,
            下钻层级序列: layers.map((layer) => ({ ...layer })),
          },
        }),
      })
      const payload = await parsePayload(response)
      setNotice(payload.message ?? '下钻路径已登记')
      setShowCreate(false)
      setPathCode('')
      setStartDimension('')
      setLayers([emptyLayer()])
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '下钻路径登记失败')
    } finally {
      setSubmitting(false)
    }
  }

  function moveLayer(index: number, offset: number) {
    setLayers((prev) => {
      const next = [...prev]
      const target = index + offset
      if (target < 0 || target >= next.length) return prev
      const [item] = next.splice(index, 1)
      next.splice(target, 0, item)
      return next
    })
  }

  async function toggleAgg(row: Row) {
    if (expandedId === row.id) {
      setExpandedId(null)
      return
    }
    setExpandedId(row.id)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/aggregation`)
      if (!response.ok) {
        throw new Error('分层聚合明细读取失败')
      }
      const detail = (await response.json()) as AggDetail
      setAggDetails((prev) => ({ ...prev, [row.id]: detail }))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分层聚合明细读取失败')
    }
  }

  return (
    <section className="page" data-module="drilldown">
      <header className="page-head">
        <div>
          <h2>多维下钻管理</h2>
          <p className="page-desc">定义多维度数据下钻的分析路径，从高层汇总到明细的逐层探索。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowCreate((prev) => !prev)}>
            {showCreate ? '收起登记' : '登记下钻路径'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出多维下钻清单
          </button>
        </div>
      </header>

      {showCreate && (
        <form
          className="create-panel"
          onSubmit={(event) => {
            event.preventDefault()
            void submitCreate()
          }}
        >
          <div className="create-row">
            <label className="filter-item">
              <span>路径编号</span>
              <input value={pathCode} placeholder="如 DRIL-1001" onChange={(event) => setPathCode(event.target.value)} />
            </label>
            <label className="filter-item">
              <span>起始维度</span>
              <input value={startDimension} placeholder="需与第一层维度一致" onChange={(event) => setStartDimension(event.target.value)} />
            </label>
          </div>
          <div className="layer-editor">
            <span className="layer-title">下钻层级（顺序即下钻顺序，保存后不会乱序）</span>
            {layers.map((layer, index) => (
              <div className="layer-row" key={index}>
                <span className="layer-no">第{index + 1}层</span>
                <input
                  value={layer.维度}
                  placeholder="维度"
                  onChange={(event) => setLayers((prev) => prev.map((item, i) => (i === index ? { ...item, 维度: event.target.value } : item)))}
                />
                <select
                  value={layer.聚合粒度}
                  onChange={(event) => setLayers((prev) => prev.map((item, i) => (i === index ? { ...item, 聚合粒度: event.target.value } : item)))}
                >
                  {GRANULARITIES.map((item) => (
                    <option key={item} value={item}>{item}</option>
                  ))}
                </select>
                <input
                  value={layer.默认度量}
                  placeholder="默认度量"
                  onChange={(event) => setLayers((prev) => prev.map((item, i) => (i === index ? { ...item, 默认度量: event.target.value } : item)))}
                />
                <input
                  value={layer.缓存秒数}
                  placeholder="缓存秒数"
                  onChange={(event) => setLayers((prev) => prev.map((item, i) => (i === index ? { ...item, 缓存秒数: event.target.value } : item)))}
                />
                <input
                  value={layer.数据新鲜度}
                  placeholder="数据新鲜度，如 5分钟"
                  onChange={(event) => setLayers((prev) => prev.map((item, i) => (i === index ? { ...item, 数据新鲜度: event.target.value } : item)))}
                />
                <button className="link" type="button" onClick={() => moveLayer(index, -1)}>上移</button>
                <button className="link" type="button" onClick={() => moveLayer(index, 1)}>下移</button>
                <button
                  className="link"
                  type="button"
                  onClick={() => setLayers((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== index) : prev))}
                >
                  删除
                </button>
              </div>
            ))}
            <button className="btn ghost" type="button" onClick={() => setLayers((prev) => [...prev, emptyLayer()])}>
              添加层级
            </button>
          </div>
          <div className="create-actions">
            <button className="btn primary" type="submit" disabled={submitting}>
              {submitting ? '提交中…' : '提交登记'}
            </button>
            <span className="hint-text">启用时会校验层级重复、跨层粒度递进与起始维度一致</span>
          </div>
        </form>
      )}

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>路径编号</span>
          <input value={keyword} placeholder="按路径编号检索" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">全部状态</option>
            {statuses.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setKeyword(''); setStatusFilter('') }}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Fragment key={String(row.id)}>
              <tr>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>{row.status}</td>
                <td>v{row.版本 ?? 1}</td>
                <td className="row-actions">
                  {(STATUS_ACTIONS[String(row.status)] ?? []).map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                  {!row.层级明细 && (
                    <button className="link" type="button" onClick={() => void convertLegacy(row)}>
                      兼容转换
                    </button>
                  )}
                  <button className="link" type="button" onClick={() => void toggleAgg(row)}>
                    {expandedId === row.id ? '收起聚合' : '聚合详情'}
                  </button>
                </td>
              </tr>
              {expandedId === row.id && (
                <tr>
                  <td colSpan={columns.length + 3}>
                    <AggPanel detail={aggDetails[row.id]} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无多维下钻数据，可先登记下钻路径</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条多维下钻记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && (
          <span className="error-text">
            {errorMessage}
            <button className="link" type="button" onClick={() => void reload()}>重试</button>
          </span>
        )}
      </footer>
    </section>
  )
}

function AggPanel({ detail }: { detail?: AggDetail }) {
  if (!detail) {
    return <div className="agg-panel">分层聚合明细加载中…</div>
  }
  if (detail.layers.length === 0) {
    return <div className="agg-panel empty-state">{detail.说明 || '暂无结果：没有可追溯的分层聚合数据'}</div>
  }
  return (
    <div className="agg-panel">
      <div className="agg-head">
        <span>路径状态：{detail.状态}</span>
        <span>快照版本：v{detail.快照版本}</span>
        <span>快照映射版本：v{detail.映射版本}</span>
        <span>当前映射版本：v{detail.当前映射版本}</span>
        <span className={detail.口径一致 ? 'ok-text' : 'error-text'}>
          {detail.口径一致 ? '维度口径一致' : '维度口径已变化，请核对快照与当前映射'}
        </span>
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>层级</th>
            <th>维度</th>
            <th>标准维度</th>
            <th>聚合粒度</th>
            <th>默认度量</th>
            <th>度量值</th>
            <th>缓存秒数</th>
            <th>数据新鲜度</th>
            <th>来源批次</th>
            <th>数据状态</th>
            <th>生成时间</th>
          </tr>
        </thead>
        <tbody>
          {detail.layers.map((layer) => (
            <tr key={layer.层级序号}>
              <td>第{layer.层级序号}层</td>
              <td>{layer.维度}</td>
              <td>{layer.标准维度 || '未映射'}</td>
              <td>{layer.聚合粒度}</td>
              <td>{layer.默认度量}</td>
              <td>{layer.度量值}</td>
              <td>{layer.缓存秒数}</td>
              <td>{layer.数据新鲜度}</td>
              <td>{layer.来源批次 || '—'}</td>
              <td>{layer.数据状态}</td>
              <td>{layer.生成时间}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
