import re
from abc import ABC, abstractmethod
from pyutilb.util import md5, parse_func


# 任务命名者, 给step/dag task命名
class TaskNamer(ABC):

    def __init__(self):
        self.task2name = {}

    # 获得任务名, 有缓存, 统一入口
    def get_name(self, task: str):
        if task not in self.task2name:
            self.task2name[task] = self.build_name(task)
        return self.task2name[task]

    # 构建任务名, 无缓存, 子类实现
    @abstractmethod
    def build_name(self, task: str):
        pass

# md5做任务命名者
class Md5TaskNamer(TaskNamer):

    def build_name(self, task: str):
        return md5(task)

# 转中划线的任务命名者
class MidlineTaskNamer(TaskNamer):

    def build_name(self, task: str):
        name = re.sub(r'[\(,]', '-', task)
        return re.sub(r'[\s\)]', '', name)

# 递增的任务命名者，如 Task1, Task2
class IncrTaskNamer(TaskNamer):

    def __init__(self, pref='Task', incr_letter=False):
        '''
        构造函数
        :param pref: 前缀
        :param incr_letter: 是否递增字母，否则递增数字
        '''
        super().__init__()
        self.pref = pref
        self.incr_letter = incr_letter
        self.counter = 0

    def build_name(self, task: str):
        # 计数+1
        self.counter += 1
        # 拼接前缀+计数
        if self.incr_letter:
            return self.pref + chr(self.counter + 64)  # 64是'A'的ASCII值
        return self.pref + str(self.counter)

# 同名函数递增的任务命名者，如 call, call2
class FuncIncrTaskNamer(TaskNamer):

    def __init__(self):
        '''
        构造函数
        :param pref: 前缀
        :param incr_letter: 是否递增字母，否则递增数字
        '''
        super().__init__()
        self.counters = {}

    def build_name(self, task: str):
        # 拆分出函数名
        func, _ = parse_func(task, True)
        # 第一次，原样返回函数名
        if func not in self.counters:
            self.counters[func] = 1
            return func

        # 计数+1
        self.counters[func] += 1
        # 拼接函数名+计数
        return func + str(self.counters[func])

if __name__ == '__main__':
    task1 = 'xxx(1)'
    task2 = 'xxx(2)'
    classes = [Md5TaskNamer, MidlineTaskNamer, IncrTaskNamer]
    for c in classes:
        n = c()
        print(n.get_name(task1))
        print(n.get_name(task2))