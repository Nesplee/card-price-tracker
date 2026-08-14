import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import { Catalogue } from './pages/Catalogue'
import { DetailCarte } from './pages/DetailCarte'
import { MaCollection } from './pages/MaCollection'

export default function App() {
  return (
    <BrowserRouter>
      <nav className="nav">
        <span className="nav-brand">Card Price Tracker</span>
        <div className="nav-links">
          <NavLink to="/" end className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
            Catalogue
          </NavLink>
          <NavLink to="/collection" className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
            Ma collection
          </NavLink>
        </div>
      </nav>
      <Routes>
        <Route path="/" element={<Catalogue />} />
        <Route path="/cartes/:cardId" element={<DetailCarte />} />
        <Route path="/collection" element={<MaCollection />} />
      </Routes>
    </BrowserRouter>
  )
}
