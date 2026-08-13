import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { Catalogue } from './pages/Catalogue'
import { DetailCarte } from './pages/DetailCarte'
import { MaCollection } from './pages/MaCollection'

export default function App() {
  return (
    <BrowserRouter>
      <nav>
        <Link to="/">Catalogue</Link>
        {' | '}
        <Link to="/collection">Ma collection</Link>
      </nav>
      <Routes>
        <Route path="/" element={<Catalogue />} />
        <Route path="/cartes/:cardId" element={<DetailCarte />} />
        <Route path="/collection" element={<MaCollection />} />
      </Routes>
    </BrowserRouter>
  )
}
