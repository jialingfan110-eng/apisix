/*
Licensed to the Apache Software Foundation (ASF) under one or more
contributor license agreements. See the NOTICE file distributed with
this work for additional information regarding copyright ownership.
The ASF licenses this file to You under the Apache License, Version 2.0
(the "License"); you may not use this file except in compliance with
the License. You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Discard the credential before a browser can retain this page in its back/forward cache.
function discardKey() {
  const key = document.querySelector("[data-one-time-key]");
  if (key) {
    key.value = "";
    key.textContent = "";
    key.remove();
  }
  const notice = document.querySelector(".notice");
  if (notice) notice.textContent = "完整 key 已展示，不会再次显示。请返回员工列表。";
}
window.addEventListener("pagehide", discardKey);
window.addEventListener("pageshow", (event) => {
  if (event.persisted) discardKey();
});
