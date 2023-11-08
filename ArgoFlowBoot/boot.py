#!/usr/bin/python3
# -*- coding: utf-8 -*-

import json
import os
import re
from pyutilb.util import *
from pyutilb.file import *
from pyutilb.cmd import *
from pyutilb import YamlBoot, BreakException
from pyutilb.log import log
from ArgoFlowBoot.task_namer import *

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
            'flow': self.flow,
            'labels': self.labels,
            'args': self.args,
            'vc': self.vc,
            'artifacts': self.artifacts,
            'templates': self.templates,
        }
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
        self._flow = '' # 流程名
        self._labels = {}  # 记录标签
        self._args = {}  # 记录流程级传参
        self._templates = {}  # 记录模板，key是模板名，value是模板定义
        self._template_inputs = {} # 记录模板的输入参数名
        self._template_outputs = {} # 记录模板的输出参数名
        self._vc_templates = [] # 记录vs模板
        self._vc_mounts = {} # 记录vs挂载路径，key是vc名，value是挂载路径
        self._arts = {} # 记录工件映射的路径，key是工件名，value是挂载路径

        # 模板主题构建器
        self.template_body_builders = {
            'container': self.build_container,
            'script': self.build_script,
            'steps': self.build_steps,
            'suspend': self.build_suspend,
            'apply': self.build_apply,
            'delete': self.build_delete,
            'dag': self.build_dag,
        }
        # python版本
        py_versions = '3.6/3.7/3.8/3.9/3.10/3.11'.split('/')
        for version in py_versions:
            self.template_body_builders['python'+version] = self.wrap_build_python(version)

        # 任务命名者
        self.namer = FuncIncrTaskNamer()

    # 清空app相关的属性
    def clear_app(self):
        self._flow = None  # 流程名
        set_var('flow', None)
        self._labels = {}  # 记录标签
        self._args = {}  # 记录流程级传参
        self._templates = {}  # 记录模板，key是模板名，value是模板定义
        self._template_inputs = {} # 记录模板的输入参数名
        self._template_outputs = {} # 记录模板的输出参数名
        self._vc_templates = [] # 记录vs模板
        self._vc_mounts = {} # 记录vs挂载路径，key是vc名，value是挂载路径
        self._arts = {} # 记录工件映射的路径，key是工件名，value是挂载路径

    def save_yaml(self, data):
        '''
        保存yaml
        :param data 资源数据
        '''
        # 检查流程名
        if self._flow is None:
            raise Exception(f"生成工作流文件失败: 没有指定流程")
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

    @replace_var_on_params
    def labels(self, lbs):
        '''
        设置流程标签
        :param lbs:
        :return:
        '''
        self._labels.update(lbs)

    def build_labels(self, lbs = None):
        if not lbs:
            return self._labels
        # 合并标签
        return dict(lbs, **self._labels)

    def build_flow(self):
        # 入口为main
        ep = "main"
        if ep not in self._templates:
            raise Exception("未定义入口模板: main")

        yaml = {
            "metadata": {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Workflow",
                "generateName": self._flow + '-',
                "labels": self.build_labels()
            },
            "spec": {
                "arguments": self._args,
                "entrypoint": ep,
                "volumeClaimTemplates": self._vc_templates,
                "templates": list(self._templates.values()),
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
        if not self._vc_mounts:
            return None
        return [{ "name": k, "mountPath": v } for k, v in self._vc_mounts.items()]

    @replace_var_on_params
    def vc(self, mounts):
        '''
        构建持久卷声明
        :params mounts 多行，格式为
                    work:/work:1Gi 定义名为work的PVC存储，请求了 1GB 的存储空间，并挂载到容器的/work
        '''
        if mounts is None or len(mounts) == 0:
            return None
        if isinstance(mounts, str):
            mounts = [mounts]

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
            self._vc_templates.append(vc)

            # 3 记录挂载路径
            self._vc_mounts[name] = mount_path

    @replace_var_on_params
    def artifacts(self, arts):
        '''
        共享文件(工件), 所有任务都可读写
        :param option: 参考 ArgoFlowBoot/example/artifact-type.yml
                      https://argoproj.github.io/argo-workflows/walk-through/artifacts/
                      https://argoproj.github.io/argo-workflows/walk-through/hardwired-artifacts/
        :return:
        '''
        if not arts:
            return None

        for name, option in arts.items():
            if isinstance(option, str):
                option = {
                    'path': option
                }
            self._arts[name] = option # 记录工件映射的路径
            set_var('@'+name, option['path']) # 设置变量

    @replace_var_on_params
    def args(self, args):
        '''
        流程级传参, 参考 https://argoproj.github.io/argo-workflows/walk-through/parameters/
        :param args:
        :return:
        '''
        self._args = self.build_dict_args(args, 'flow')

    def ref_pod_field(self, field):
        '''
        在给环境变量赋时值，注入Pod信息
          参考 https://blog.csdn.net/skh2015java/article/details/109229107
        :param field Pod信息字段，仅支持 metadata.name, metadata.namespace, metadata.uid, spec.nodeName, spec.serviceAccountName, status.hostIP, status.podIP, status.podIPs
        '''
        return {
            "fieldRef": {
                "fieldPath": field
            }
        }

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
        name, key = key.split('.')
        return {
            "configMapKeyRef":{
              "name": name, # The ConfigMap this value comes from.
              "key": key # The key to fetch.
            }
        }

    def ref_secret(self, key):
        '''
        在给环境变量赋值时，注入secret信息
        :param key
        '''
        name, key = key.split('.')
        return {
            "secretKeyRef":{
              "name": name, # The Secret this value comes from.
              "key": key # The key to fetch.
            }
        }

    def templates(self, options):
        # 模板名:模板配置
        for name, option in options.items():
            self.build_template(name, option)

    def build_template_body(self, option):
        # 逐个匹配模板类型，并调用对应的模板主体构建器
        for type, builder in self.template_body_builders.items():
            if type in option:
                return builder(option[type])

        return None

    def build_container(self, option):
        ret = {
            "container": {
                "image": get_and_del_dict_item(option, "image", "alpine:3.6"),
                "command": self.fix_command(get_and_del_dict_item(option, "command")),
                "args": self.fix_command_args(get_and_del_dict_item(option, "args")),
                "volumeMounts": self.build_volume_mounts(),
                "env": self.build_env(get_and_del_dict_item(option, 'env')),
                **option
            },
        }
        del_dict_none_item(ret["container"])
        return ret

    def build_template(self, name: str, option: dict):
        '''
        构建任务模板
        :param name: 任务模板名，函数调用的形式
        :param option: 任务模板选项
        :return:
        '''
        # 解析函数调用
        name, args = parse_func(name, True)
        # 构建输入
        self._template_inputs[name] = args # 记录模板的输入参数名
        # push_vars_stack() # 变量入栈
        inputs = self.build_list_args(args, 'inputs') # 构建输入，会增加变量
        if 'steps' not in option: # steps延迟替换变量
            option = replace_var(option, False) # 替换变量
        # 构建输出
        out = get_and_del_dict_item(option, 'out')
        outputs = self.build_dict_args(out, 'outputs') # 构建输出
        if out:
            self._template_outputs[name] = out.keys()  # 记录模板的输出参数名
        # 构建主体
        body = self.build_template_body(option)
        if body is None:
            raise Exception(f'不确定任务[{name}]的类型')
        tpl = {
            "name": name,
            "inputs": inputs,
            "outputs": outputs,
            **body
        }
        # pop_vars_stack() # 变量出栈
        del_dict_none_item(tpl)
        self._templates[name] = tpl

    def build_dict_args(self, args: dict, type: str):
        '''
        构建inputs/outputs/flow/call(调用模板)等的dict类型的参数
        输入参数参考 https://argoproj.github.io/argo-workflows/walk-through/parameters/
        输出参数参考 https://argoproj.github.io/argo-workflows/walk-through/output-parameters/

        :param args:
        :param type: 参数类型: inputs/outputs/flow/call(调用模板)
        :return:
        '''
        if not args:
            return None
        # 拆分parameters与artifacts
        params = {}
        arts = {}
        for k, v in args.items():
            if k.startswith('@'): # artifacts
                arts[k] = v
            else: # parameters
                params[k] = v
                if type == 'inputs' or type == 'flow':
                    set_var(k, '{{inputs.parameters.' + k + '}}') # 设变量
        # 构建参数
        ret = {
            "parameters": self.build_params(params),
            "artifacts": self.build_artifacts(arts, type),
        }
        del_dict_none_item(ret)
        return ret

    def build_list_args(self, args: list, type: str):
        '''
        构建inputs/outputs/flow/call(调用模板)等的list类型的参数
        输入参数参考 https://argoproj.github.io/argo-workflows/walk-through/parameters/
        输出参数参考 https://argoproj.github.io/argo-workflows/walk-through/output-parameters/

        :param args:
        :param type: 参数类型: inputs/outputs/flow/call(调用模板)
        :return:
        '''
        if not args:
            return None
        # 拆分parameters与artifacts
        params = []
        arts = []
        for arg in args:
            if arg.startswith('@'):  # artifacts
                arts.append(arg)
            else: # parameters
                params.append(arg)
                if type == 'inputs' or type == 'flow':
                    set_var(arg, '{{inputs.parameters.' + arg + '}}')  # 设变量
        # 构建参数
        ret = {
            "parameters": self.build_params(params),
            "artifacts": self.build_artifacts(arts, type),
        }
        del_dict_none_item(ret)
        return ret

    # 构建模板的输入参数
    def build_artifacts(self, option, type=None):
        if not option:
            return None

        if isinstance(option, list):
            return [self.build_artifact(v, None, type) for v in option]

        if isinstance(option, dict):
            return [self.build_artifact(k, v, type) for k, v in option.items()]

        raise Exception(f"无效参数选项: {option}")

    def build_artifact(self, key, value=None, type=None):
        '''
        构建工件
        :param key:
        :param value:
        :param type
        :return:
        '''
        key = key.replace('@', '')
        # 1 有明细配置dict
        if isinstance(value, dict):
            return {
                "name": key,
                **value
            }

        # 2 调用模板: 用from
        if type == 'call':
            return {
                "name": key,
                "from": value
            }

        # 3 优先用用户填的
        if value is not None:
            return {
                "name": key,
                "path": value
            }

        # 4 最后用全局配的
        return {
            "name": key,
            **self._arts[key]
        }

    # 构建模板的输入参数
    def build_params(self, option):
        if not option:
            return None

        if isinstance(option, list):
            return [self.build_param(v) for v in option]

        if isinstance(option, dict):
            return [self.build_param(k, v) for k, v in option.items()]

        raise Exception(f"无效参数选项: {option}")

    def build_param(self, key, value=None):
        if value is None:
            return {
                "name": key
            }
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

    def build_steps(self, steps):
        '''
        构建steps, 参考 https://argoproj.github.io/argo-workflows/walk-through/steps/
        :param steps:
        :return:
        '''
        # 多个步骤
        if isinstance(steps, list):
            return {
                "steps": list(map(self.build_step, steps))
            }

        # 多个步骤，带步骤名
        if isinstance(steps, dict):
            return {
                "steps": [self.build_step(step, name) for name, step in steps.items()]
            }

        # 单个步骤
        return self.build_step(steps)

    # 构建step
    #@replace_var_on_params
    def build_step(self, step, name = None):
        # list要递归，因为steps中--代表顺序执行，- 代表并行执行，也就是可能有多层list，要递归调用
        if isinstance(step, list):
            return list(map(self.build_step, step))

        # dict: template+when
        when = None
        if isinstance(step, dict):
            when = step['when']
            step = step['template']
        # 解析模板
        step = replace_var(step)
        template, args = parse_func(step, True)
        if name is None:
            name = self.namer.build_name(template) # 生成步骤名, 不缓存
        # 拼接步骤
        ret = {
            "name": name,
            "template": template
        }
        if when:
            ret['when'] = when
        if args:
            ret['arguments'] = self.build_step_call_args(template, args)
        # 输出变量
        self.build_step_out_vars(template, name, 'steps')
        return ret

    def build_step_out_vars(self, tpl_name, step_name, prefix):
        '''
        构建steps调用的输出变量
        :param tpl_name:
        :param step_name:
        :param prefix: 表达式前缀，steps模板的前缀为steps，dag模板的前缀tasks
                     steps: {{steps.generate.outputs.artifacts.out-artifact}}
                     tasks: {{tasks.generate-artifact.outputs.artifacts.hello-art}}
        :return:
        '''
        # 输出参数名
        names = self._template_outputs.get(tpl_name)
        # 遍历输出的参数，来设置变量
        vals = {}
        if names:
            for name in names:
                if name.startswith('@'):  # artifacts: {{steps.generate.outputs.artifacts.out-artifact}}
                    val = '{{' + prefix + '.' + step_name + '.outputs.artifacts.' + name[1:] + '}}'
                else:  # parameters: {{steps.generate.outputs.parameters.out-parameter}}
                    val = '{{' + prefix + '.' + step_name + '.outputs.parameters.' + name + '}}'
                vals[name] = val
        # 设置result变量，注：不建议输出参数名用result
        if 'result' not in vals:
            vals['result'] = '{{' + prefix + '.' + step_name + '.outputs.result}}'
        set_var(step_name, vals)

    # 构建steps调用的参数
    def build_step_call_args(self, tpl_name, vals):
        # 输入参数名
        names = self._template_inputs[tpl_name]
        if len(names) != len(vals):
            raise Exception(f"调用模板{tpl_name}的参数个数与声明的参数个数不一致")

        args = dict(zip(names, vals))
        return self.build_dict_args(args, 'call')

    def build_script(self, option, default_image="docker/whalesay:latest"):
        '''
        构建script模板，参考 https://argoproj.github.io/argo-workflows/walk-through/scripts-and-results/
        :param option:
        :param default_image:
        :return:
        '''
        image = get_and_del_dict_item(option, "image", default_image)
        # 命令
        cmd = get_and_del_dict_item(option, "command", "bash")
        if isinstance(cmd, str):
            cmd = [cmd]
        # 源码
        src = get_and_del_dict_item(option, "source")
        if 'file' in option:
            src = read_file(get_and_del_dict_item(option, "file"))
        ret = {
            "script": {
                "image": image,
                "command": cmd,
                "source": src,
                "env": self.build_env(get_and_del_dict_item(option, 'env')),
                **option
            }
        }
        del_dict_none_item(ret["script"])
        return ret

    # 构建容器中的环境变量
    def build_env(self, env):
        if env is None or len(env) == 0:
            return None

        ret = []
        for key, val in env.items():
            item = {
                "name": key,
            }
            if isinstance(val, (str, int, float)):
                item["value"] = str(val)
            else:
                item["valueFrom"] = val
            ret.append(item)
        return ret

    def wrap_build_python(self, version):
        def wrapper(option):
            option['command'] = 'python'
            return self.build_script(option, default_image=f"python:alpine{version}")
        return wrapper

    def build_suspend(self, option):
        if option is None:
            return {
                "suspend": {}
            }

        if isinstance(option, (int, str)):
            duration = option
        else:
            duration = option['duration']
        return {
            "suspend": {
                "duration": str(duration) # Must be a string. Default unit is seconds. Could also be a Duration, e.g.: "2m", "6h", "1d"
            }
        }

    # 流程资源文件
    def build_apply(self, option):
        return self.build_res_action("apply", option)

    # 删除文件
    def build_delete(self, option):
        return self.build_res_action("delete", option)

    # 操作资源
    def build_res_action(self, action, option):
        # 源码
        src = get_and_del_dict_item(option, "manifest")
        if 'file' in option:
            src = read_file(get_and_del_dict_item(option, "file"))
        return {
            "resource": {
                "action": action,
                "manifest": src
            }
        }

    def build_dag(self, deps):
        '''
        构建dag(依赖关系), 参考 https://argoproj.github.io/argo-workflows/walk-through/dag/
        :param deps:
        :return:
        '''
        tasks = []
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            # 去掉空格
            #dep = dep.replace(' ', '')
            dep = re.sub(r'\s*(->|;|,)\s*', lambda m: m.group(1), dep)
            if not dep:
                continue
            items = dep.split('->') # 分割每个点
            items = list(map(lambda x: x.split(';'), items)) # 点中有点, 分号分割
            # 首个点: 无依赖
            for node in items[0]:
                name = self.namer.get_name(node)
                task = {
                    "name": name,
                    "template": node
                }
                tasks.append(task)
            # 后续的点: 依赖于前一个点
            for i in range(1, len(items)):
                item = items[i]
                for node in item:
                    task = {
                        "name": self.namer.get_name(node),
                        "dependencies": list(map(self.namer.get_name, items[i - 1])),
                        "template": node
                    }
                    tasks.append(task)

        return {
            "dag": {
                "tasks": tasks
            }
        }

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
    main()
    # data = read_yaml('/home/shi/code/python/ArgoFlowBoot/example/test.yml')
    # print(json.dumps(data))
