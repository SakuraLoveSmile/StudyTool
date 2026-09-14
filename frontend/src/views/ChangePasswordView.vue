<template>
  <div class="mx-auto mt-12 max-w-md rounded-xl border border-slate-200 bg-white p-8 shadow-sm">
    <h1 class="text-2xl font-bold text-slate-900">修改密码</h1>
    <p class="mt-2 text-sm text-slate-500">首次登录需要设置新密码</p>
    <form class="mt-6 space-y-4" @submit.prevent="submit">
      <label class="block text-sm font-medium text-slate-700">当前密码<input v-model="currentPassword" required type="password" autocomplete="current-password" class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2" /></label>
      <label class="block text-sm font-medium text-slate-700">新密码<input v-model="newPassword" required minlength="8" type="password" autocomplete="new-password" class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2" /></label>
      <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
      <button :disabled="loading" class="w-full rounded-lg bg-blue-600 px-4 py-2.5 font-medium text-white hover:bg-blue-700 disabled:opacity-50">{{ loading ? '保存中...' : '保存新密码' }}</button>
    </form>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue'; import { useRoute, useRouter } from 'vue-router'; import { useAuth } from '../composables/useAuth';
const currentPassword = ref(''); const newPassword = ref(''); const error = ref(''); const router = useRouter(); const route = useRoute(); const auth = useAuth(); const loading = auth.loading;
const submit = async () => { error.value = ''; try { await auth.changePassword(currentPassword.value, newPassword.value); await router.replace(String(route.query.redirect || '/today')); } catch (e) { error.value = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '修改密码失败'; } };
</script>
