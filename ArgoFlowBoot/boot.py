#!/usr/bin/python3
# -*- coding: utf-8 -*-

import fnmatch
import hashlib
import json
import os
import re
from itertools import groupby
from urllib import parse
from pyutilb import util
from pyutilb.util import *
from pyutilb.file import *
from pyutilb.cmd import *
from pyutilb import YamlBoot, BreakException
from pyutilb.log import log
from dotenv import dotenv_values
from kubernetes import client, config

# 调整变量的正则, 支持@前缀, 表示artifact路径变量
util.reg_var_pure = '@?[\w\d_]+'

'''
argo flow配置生成的基于yaml的启动器
'''
class Boot(YamlBoot):

    def __init__(self, output_dir):
        super().__init__()
        self.output_dir = os.path.abspath(output_dir or 'out')
        # step_dir作为当前目录
        self.step_dir_as_cwd = True
        # 不需要输出统计结果到stat.yml
        self.stat_dump = False
        # 动作映射函数
        actions = {
            'ns': self.ns,
            'flow': self.flow,
            'labels': self.labels,
        }
        # python版本
        py_versions = '3.6/3.7/3.8/3.9/3.10/3.11'.split('/')
        for version in py_versions:
            actions['python'+version] = self.wrap_python(version)
        self.add_actions(actions)
        # 自定义函数
        funcs = {
            'ref_pod_field': self.ref_pod_field,
            'ref_resource_field': self.ref_resource_field,
            'ref_config': self.ref_config,
            'ref_secret': self.ref_secret,
        }
        custom_funs.update(funcs)

        # flow作用域的属性，跳出flow时就清空
        self._flow = '' # 应用名
        self._labels = {}  # 记录标签
        self._templates = {}  # 记录模板，key是模板名，value是模板定义
        self._template_args = {} # 记录模板名对参数
        self._vc_mounts = {} # 记录vs挂载路径，key是vc名，value是挂载路径
        self._arts = {} # 记录工件映射的路径，key是工件名，value是挂载路径

    # 清空app相关的属性
    def clear_app(self):
        self._flow = None  # 应用名
        set_var('flow', None)
        self._labels = {}  # 记录标签
        self._templates = {}  # 记录模板，key是模板名，value是模板定义
        self._template_args = {} # 记录模板名对参数
        self._vc_mounts = {} # 记录vs挂载路径，key是vc名，value是挂载路径
        self._arts = {} # 记录工件映射的路径，key是工件名，value是挂载路径

    def save_yaml(self, data):
        '''
        保存yaml
        :param data 资源数据
        '''
        # 拼接文件名
        # 检查流程名
        if self._flow is None:
            raise Exception(f"生成工作流文件失败: 没有指定应用")
        file = f"{self._flow}.yml"
        # 转yaml
        if isinstance(data, list): # 多个资源
            data = list(map(yaml.dump, data))
            data = "\n---\n\n".join(data)
        elif not isinstance(data, str):
            data = yaml.dump(data)
        # 创建目录
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        # 保存文件
        file = os.path.join(self.output_dir, file)
        write_file(file, data)

    def print_apply_cmd(self):
        '''
        打印 kubectl apply 命令
        '''
        cmd = f'App[{self._flow}]的资源定义文件已生成完毕, 如要更新到集群中的资源请手动执行: kubectl apply --record=true -f {self.output_dir}'
        log.info(cmd)

    # --------- 动作处理的函数 --------
    def flow(self, steps, name=None):
        '''
        声明工作流，并执行子步骤
        :param steps 子步骤
        name 流程名
        '''
        # 如果应用名以@开头, 表示应用名也作为pod的主机名
        if name.startswith('@'):
            name = name[1:]
        # app名可带参数
        name = replace_var(name)
        self._flow = name
        set_var('flow', name)
        self._labels = {
            'flow': name
        }
        # 执行子步骤
        self.run_steps(steps)
        # 生成flow
        self.build_flow()
        # 打印 kubectl apply 命令
        self.print_apply_cmd()
        # 清空app相关的属性
        self.clear_app()

    def build_metadata(self, anns=None):
        meta = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "generateName": self._flow + '-',
            "labels": self.build_labels()
        }
        if anns:
            meta['annotations'] = anns
        return meta

    def build_labels(self, lbs = None):
        if not lbs:
            return self._labels
        # 合并标签
        return dict(lbs, **self._labels)

    def build_flow(self, option):
        yaml = {
          "metadata": self.build_metadata(),
          "spec": {
            "arguments": {
              "parameters": [
                {
                  "name": "message",
                  "value": "hello argo"
                }
              ]
            },
            "entrypoint": "main",
            "volumeClaimTemplates": self.build_vc(get_and_del_dict_item(option, 'vc')),
            "templates": self._templates.values(),
            "ttlStrategy": {
              "secondsAfterCompletion": 300
            },
            "podGC": {
              "strategy": "OnPodCompletion"
            }
          }
        }
        self.save_yaml(yaml)

    def build_volume_mounts(self):
        return [{ "name": k, "mountPath": k } for k, v in self._vc_mounts.items()]

    def build_volume_claims(self, mounts):
        '''
        构建持久卷声明
        :params mounts 多行，格式为
                    work:/work:1Gi 定义名为work的PVC存储，请求了 1GB 的存储空间，并挂载到容器的/work
        '''
        if mounts is None or len(mounts) == 0:
            return None
        if isinstance(mounts, str):
            mounts = [mounts]

        ret = []
        for mount in mounts:
            # 1 默认名为work
            if ':' not in mount:
                mount = f"work://:{mount}"
            # 2 拆分2段或3段
            parts = mount.split(':', 2)
            if len(parts) == 2:
                name, mount_path = parts
                storage = '100Mi' # 默认请求 100M 空间
            else:
                name, mount_path, storage = parts
            # 构建vc
            vc = {
                "metadata": {
                    "name": name
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {
                        "requests": {
                            "storage": storage
                        }
                    }
                }
            }
            ret.append(vc)

            # 3 记录挂载路径
            self._vc_mounts[name] = mount_path
        return ret

    def art(self, arts):
        '''
        共享文件(工件), 所有任务都可读写
        :param option: 多行，格式为
                     test:/tmp/test.txt -- 工件名: 挂载路径
                     test:https://storage.googleapis.com/kubernetes-release/release/v1.8.0/bin/linux/amd64/kubectl -- 工件名:协议://路径
        :return:
        '''
        if arts is None or len(arts) == 0:
            return None
        if isinstance(arts, str):
            arts = [arts]

        ret = []
        for art in arts:
            # 1 解析工件名:挂载路径
            if ':' not in art:
                raise Exception(f'无效工件参数: {art}')
            name, art = art.split(':')
            # 2 解析协议：协议格式参考函数注释
            protocol = None
            if "://" in art: # 有协议
                #  mat = re.search('(\w+)://([\w\d\._]*)(/.+):(.+)', art)
                mat = re.search('(\w+)://(.+)', art)
                protocol = mat.group(1) # 协议
                path = mat.group(2) # 主机 + 宿主机路径
            else: # 无协议: 默认挂载
                path = art

            # 3 记录工件映射的路径
            self._arts[name] = path
            set_var('@'+name, path) # 设置变量
        return ret

    # 准备参数的变量
    def prepare_param_var(self):
        for i in range(0, 10):
            name = f"p{i}" # 变量名
            set_var(name, "{{inputs.parameters." + name + "}}")

    def ref_resource_field(self, field):
        '''
        在给环境变量赋值时，注入容器资源信息
          参考 https://blog.csdn.net/skh2015java/article/details/109229107
        :param field 容器资源信息字段，仅支持 requests.cpu, requests.memory, limits.cpu, limits.memory
        '''
        return {
            "resourceFieldRef": {
                "containerName": self._curr_container,
                "resource": field
            }
        }

    def ref_config(self, key):
        '''
        在给环境变量赋值时，注入配置信息
        :param key
        '''
        return {
            "configMapKeyRef":{
              "name": self._app, # The ConfigMap this value comes from.
              "key": key # The key to fetch.
            }
        }

    def ref_secret(self, key):
        '''
        在给环境变量赋值时，注入secret信息
        :param key
        '''
        return {
            "secretKeyRef":{
              "name": self._app, # The Secret this value comes from.
              "key": key # The key to fetch.
            }
        }

    def parse_template_func(self, expr):
        # 解析函数调用
        name, args = parse_func(expr)
        # 记录模板名对参数
        self._template_args[name] = args

    def container(self, options):
        for name, option in options:
            name, args = parse_func(name)
            inputs = self.build_inputs(args)
            tpl = {
                "name": name,
                "container": {
                    "image": get_and_del_dict_item(option, "image", "alpine:3.6"),
                    "command": self.fix_command(get_and_del_dict_item(option, "command")),
                    "args": self.fix_command_args(get_and_del_dict_item(option, "args")),
                    "volumeMounts": self.build_volume_mounts(),
                    **option
                },
                "inputs": inputs
            }
            del_dict_none_item(tpl)
            self._templates[name] = tpl

    # 构建input参数
    def build_inputs(self, args):
        if not args:
            return None
        # 拆分parameters与artifacts
        params = []
        arts = []
        for arg in args:
            if arg.startswith('@'): # artifacts
                arts.append(arg)
            else:
                params.append(arg)
                set_var(arg, '{{inputs.parameters.' + arg + '}}') # 设变量
        # 构建input参数
        return {
            "parameters": self.build_params(params),
            "artifacts": self.build_artifacts(arts),
        }

    # 构建output参数
    def build_outputs(self, args):
        if not args:
            return None
        # 拆分parameters与artifacts
        params = []
        arts = []
        for arg in args:
            if arg.startswith('@'): # artifacts
                arts.append(arg)
            else:
                params.append(arg)
        # 构建input参数
        return {
            "parameters": self.build_params(params),
            "artifacts": self.build_artifacts(arts),
        }

    # 构建模板的输入参数
    def build_artifacts(self, arts):
        return [self.build_artifact(i, v) for i, v in enumerate(arts)]

    def build_artifact(self, key):
        return {
            "name": key,
            "path": self._arts[key]
        }

    # 构建模板的输入参数
    def build_params(self, option):
        if isinstance(option, list):
            return [self.build_param(i, v) for i, v in enumerate(option)]

        if isinstance(option, dict):
            return [self.build_param(k, v) for k, v in option.items()]

        raise Exception(f"无效参数选项: {option}")

    def build_param(self, key, value):
        if isinstance(value, dict):
            return {
                "name": key,
                "valueFrom": value
            }
        return {
            "name": key,
            "value": value
        }

    def fix_command(self, cmd):
        if isinstance(cmd, str):
            # return re.split('\s+', cmd)  # 空格分割
            return ["/bin/sh", "-c", cmd] # sh修饰，不用bash(busybox里没有bash)
        return cmd

    def fix_command_args(self, args):
        if isinstance(args, str):
            return [args]
        return args

    def steps(self, options):
        for name, steps in options:
            name, args = parse_func(name)
            tpl = {
                    "name": name,
                    "steps": [
                        [{
                            "name": "flip-coin",
                            "template": "flip-coin"
                        }],
                        [{
                            "name": "heads",
                            "template": "heads",
                            "when": "{{steps.flip-coin.outputs.result}} == heads"
                        }, {
                            "name": "tails",
                            "template": "tails",
                            "when": "{{steps.flip-coin.outputs.result}} == tails"
                        }]
                    ]
                }
            self._templates[name] = tpl

    def build_step(self, step):
        if isinstance(step, list):
            return list(map(self.build_step, step))
        name, args = parse_func(step)
        ret = {
            "name": name,
            "template": name
        }
        if args:
            ret['arguments'] = {
                'parameters': self.build_call_params(name, args)
            }
        return ret

    # 构建调用的参数
    def build_call_params(self, tpl_name, vals):
        # 参数名
        names = self._template_args[tpl_name]
        if len(names) != len(vals):
            raise Exception(f"调用模板{tpl_name}的参数个数与声明的参数个数不一致")
        ret = []
        for i in range(0, len(names)):
            param = self.build_param(names[i], vals[i])
            ret.append(param)
        return ret

    def script(self, option, name):
        pass

    def wrap_python(self, version):
        def wrapper(*args):
            return self.build_python(version, *args)
        return wrapper

    def build_python(self, version, option, name):
        image = get_and_del_dict_item(option, "image", f"python:alpine{version}")
        # 源码
        src = get_and_del_dict_item(option, "source")
        if 'file' in option:
            src = read_file(get_and_del_dict_item(option, "file"))
        # 环境变量
        env = self.build_env(get_and_del_dict_item(option, 'env')) + self.build_env_file(get_and_del_dict_item(option, 'env_file'))
        if not env:
            env = None
        return {
            "name": name,
            "script": {
                "image": image,
                "command": ["python"],
                "source": src,
                "env": env,
                **option
            }
        }

    def delay(self, duration):
        return {
            "name": "delay",
            "suspend": {
                "duration": str(duration) # Must be a string. Default unit is seconds. Could also be a Duration, e.g.: "2m", "6h", "1d"
            }
        }

    # 应用资源文件
    def apply(self, option, name):
        return self.do_res_action("apply", name, option)

    # 删除文件
    def delete(self, option, name):
        return self.do_res_action("delete", name, option)

    # 操作资源
    def do_res_action(self, action, name, option):
        # 源码
        src = get_and_del_dict_item(option, "manifest")
        if 'file' in option:
            src = read_file(get_and_del_dict_item(option, "file"))
        return {
            "name": name,
            "resource": {
                "action": action,
                "manifest": src
            }
        }

    #
    def dag(self, deps, name):
        tasks = []
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            dep = dep.replace(' ', '') # 去掉空格
            if not dep:
                continue
            items = dep.split('->') # 分割每个点
            items = list(map(lambda x: x.split(','), items)) # 点中有点, 逗号分割
            # 首个点: 无依赖
            for node in items[0]:
                task = {
                    "name": node,
                    "template": node
                }
                tasks.append(task)
            # 后续的点: 依赖于前一个点
            for i in range(1, len(items)):
                item = items[i]
                for node in item:
                    task = {
                        "name": node,
                        "dependencies": items[i-1],
                        "template": node
                    }
                    tasks.append(task)

        return [{
            "name": name,
            "dag": {
                "tasks": tasks
            }
        }]

# cli入口
def main():
    # 读元数据：author/version/description
    dir = os.path.dirname(__file__)
    meta = read_init_file_meta(dir + os.sep + '__init__.py')
    # 步骤配置的yaml
    step_files, option = parse_cmd('ArgoFlowBoot', meta['version'])
    if len(step_files) == 0:
        raise Exception("Miss step config file or directory")
    # 基于yaml的执行器
    boot = Boot(option.output)
    try:
        # 执行yaml配置的步骤
        boot.run(step_files)
    except Exception as ex:
        log.error(f"Exception occurs: current step file is %s", boot.step_file, exc_info=ex)
        raise ex


if __name__ == '__main__':
    # main()
    data = read_yaml('/home/shi/code/python/ArgoFlowBoot/example/test.yml')
    print(json.dumps(data))
