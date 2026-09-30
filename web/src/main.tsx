import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import AppFrame from './components/AppFrame'
import './styles.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('缺少 #root 挂载点。')
}

createRoot(container).render(
  <StrictMode>
    <HashRouter>
      <AppFrame />
    </HashRouter>
  </StrictMode>,
)
