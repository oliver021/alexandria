import { mount } from 'svelte'
import './app.css'
import App from './App.svelte'
import { FRAMED_MESSAGE, isFramed } from './lib/framing.js'

const target = document.getElementById('app')

if (!target) {
  throw new Error('The #app element is missing from index.html')
}

if (isFramed()) {
  target.textContent = FRAMED_MESSAGE
} else {
  mount(App, { target })
}
