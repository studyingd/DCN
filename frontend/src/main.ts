import { createApp } from 'vue'
import { createPinia } from 'pinia'
// Element Plus components need the dark CSS variable preset as well as the
// project tokens; without it, table/drawer/description/date-picker surfaces
// fall back to the library's light defaults.
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/tokens.css'
import './styles/utilities.css'
import './styles/foundation.css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(createPinia())

app.use(router)
app.mount('#app')
