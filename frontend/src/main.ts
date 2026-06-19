import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
// Element Plus base styles are auto-injected by the resolver, but the dark-mode
// CSS variables must be imported explicitly so toggling html.dark recolors.
import 'element-plus/theme-chalk/dark/css-vars.css'

const app = createApp(App)
app.use(router)
app.mount('#app')
