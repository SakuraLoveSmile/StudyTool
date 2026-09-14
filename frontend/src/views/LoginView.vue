<template>
  <div class="mx-auto mt-12 max-w-md rounded-xl border border-slate-200 bg-white p-8 shadow-sm">
    <h1 class="text-2xl font-bold text-slate-900">登录</h1>
    <p class="mt-2 text-sm text-slate-500">登录后继续你的学习记录</p>
    <form class="mt-6 space-y-4" @submit.prevent="submit">
      <label class="block text-sm font-medium text-slate-700">用户名<input v-model.trim="username" required autocomplete="username" class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2" /></label>
      <label class="block text-sm font-medium text-slate-700">密码<input v-model="password" required type="password" autocomplete="current-password" class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2" /></label>
      <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
      <button :disabled="loading" class="w-full rounded-lg bg-blue-600 px-4 py-2.5 font-medium text-white hover:bg-blue-700 disabled:opacity-50">{{ loading ? '登录中...' : '登录' }}</button>
    </form>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuth } from '../composables/useAuth';
const username = ref(''); const password = ref(''); const error = ref('');
const router = useRouter(); const route = useRoute(); const auth = useAuth();
const loading = auth.loading;
const submit = async () => { error.value = ''; try { await auth.login(username.value, password.value); await router.replace(auth.user.value?.must_change_password ? { name: 'change-password', query: route.query } : String(route.query.redirect || '/today')); } catch { error.value = auth.errorMessage.value; } };
</script>
